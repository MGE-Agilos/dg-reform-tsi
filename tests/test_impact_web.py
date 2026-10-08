import json

from haystack.components.embedders import MockTextEmbedder

from test_chat_agent import POLICY_AREAS, ScriptedGenerator, chat_settings
from test_impact_report import REPORT
from test_impact_service import projects_dir
from test_impact_tools import FakeEngine, FakeFetcher
from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.web import create_app
from tsi_agent.impact.catalog import load_catalog
from tsi_agent.impact.pipeline import build_impact_pipeline


def client(tmp_path, replies, scorer=None):
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    emb = MockTextEmbedder(model="mock", dimension=8)
    chat = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ScriptedGenerator([]), text_embedder=emb)
    impact = build_impact_pipeline(s, chat_generator=ScriptedGenerator(replies), text_embedder=emb,
                                   search_engine=FakeEngine([("News", "https://n.eu", "x")]),
                                   page_fetcher=FakeFetcher())
    app = create_app(s, pipeline=chat, impact_pipeline=impact, catalog=load_catalog(projects_dir(tmp_path)),
                     scorer=scorer)
    return app, app.test_client()


def test_projects_search_and_page_tabs(tmp_path):
    app, c = client(tmp_path, [])
    res = c.get("/api/projects?q=energy")
    assert res.status_code == 200 and res.get_json()["projects"][0]["project_id"] == "energy-regulation-academy"
    page = c.get("/").data
    assert b"Impact analysis" in page and b"/api/impact" in page


def test_impact_job_lifecycle(tmp_path):
    scorer = ScriptedGenerator([json.dumps({"impact_score": 8, "impact_rationale": "Outcomes [1]."})])
    app, c = client(tmp_path, [[("web_search", {"query": "q"})], json.dumps(REPORT)], scorer=scorer)
    assert c.post("/api/impact", json={"project_id": "unknown"}).status_code == 404
    assert c.post("/api/impact", json={"project_id": "energy-regulation-academy"}).status_code == 202
    app.config["IMPACT_JOBS"].wait(10)
    st = c.get("/api/impact/job").get_json()
    assert st["state"] == "done" and st["result"]["body"].startswith("# Impact analysis")
    assert "Searching the web: q" in st["steps"]
    data = c.get("/api/impact/reports").get_json()
    reports = data["reports"]
    assert reports[0]["project_id"] == "energy-regulation-academy"
    assert reports[0]["impact_score"] == 8 and reports[0]["impact_level"] == "strong"
    assert data["summary"] == {"strong": 1, "moderate": 0, "weak": 0, "not_rated": 0}
    one = c.get("/api/impact/reports/energy-regulation-academy").get_json()
    assert one["front"]["confidence"] == "medium" and one["body"].startswith("# Impact analysis")
    assert c.get("/api/impact/reports/nope").status_code == 404
    assert c.get("/api/impact/reports/..%2Fsecret").status_code == 404


def test_impact_conflict_when_running(tmp_path):
    app, c = client(tmp_path, [])
    jobs = app.config["IMPACT_JOBS"]
    jobs._state["state"] = "running"
    assert c.post("/api/impact", json={"project_id": "energy-regulation-academy"}).status_code == 409


def test_impact_unavailable_without_pipeline(tmp_path):
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    chat = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ScriptedGenerator([]),
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    c = create_app(s, pipeline=chat).test_client()
    assert c.post("/api/impact", json={"project_id": "x"}).status_code == 503
    res = c.get("/api/impact/job")
    assert res.status_code == 503 and res.get_json()["error"] == "impact analysis not available"
    assert c.get("/api/projects").get_json() == {"projects": []}
    assert c.get("/api/impact/reports").get_json() == {
        "reports": [], "summary": {"strong": 0, "moderate": 0, "weak": 0, "not_rated": 0}}


def test_impact_reports_summary_counts_levels_and_not_rated(tmp_path):
    app, c = client(tmp_path, [])
    impact = tmp_path / "impact"
    impact.mkdir()
    files = {
        "never": "project_id: never\ngenerated: '2026-01-01'",
        "old-scale": "project_id: old-scale\nimpact_score: 1\nimpact_category: positive",
        "weak": "project_id: weak\nimpact_score: 3\nimpact_level: weak",
        "zero": "project_id: zero\nimpact_score: 0\nimpact_level: weak",
        "mid": "project_id: mid\nimpact_score: 4\nimpact_level: moderate",
        "top": "project_id: top\nimpact_score: 10\nimpact_level: strong",
        "out": "project_id: out\nimpact_score: 11\nimpact_level: strong",
        "text": "project_id: text\nimpact_score: '7'\nimpact_level: strong",
    }
    for name, fm in files.items():
        (impact / f"{name}.md").write_text(f"---\n{fm}\n---\n# R", encoding="utf-8")
    data = c.get("/api/impact/reports").get_json()
    assert data["summary"] == {"strong": 1, "moderate": 1, "weak": 2, "not_rated": 4}
    never = next(r for r in data["reports"] if r["project_id"] == "never")
    assert "impact_score" not in never
