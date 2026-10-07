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


def client(tmp_path, replies):
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    emb = MockTextEmbedder(model="mock", dimension=8)
    chat = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ScriptedGenerator([]), text_embedder=emb)
    impact = build_impact_pipeline(s, chat_generator=ScriptedGenerator(replies), text_embedder=emb,
                                   search_engine=FakeEngine([("News", "https://n.eu", "x")]),
                                   page_fetcher=FakeFetcher())
    app = create_app(s, pipeline=chat, impact_pipeline=impact, catalog=load_catalog(projects_dir(tmp_path)))
    return app, app.test_client()


def test_projects_search_and_page_tabs(tmp_path):
    app, c = client(tmp_path, [])
    res = c.get("/api/projects?q=energy")
    assert res.status_code == 200 and res.get_json()["projects"][0]["project_id"] == "energy-regulation-academy"
    page = c.get("/").data
    assert b"Impact analysis" in page and b"/api/impact" in page


def test_impact_job_lifecycle(tmp_path):
    app, c = client(tmp_path, [[("web_search", {"query": "q"})], json.dumps(REPORT)])
    assert c.post("/api/impact", json={"project_id": "unknown"}).status_code == 404
    assert c.post("/api/impact", json={"project_id": "energy-regulation-academy"}).status_code == 202
    app.config["IMPACT_JOBS"].wait(10)
    st = c.get("/api/impact/job").get_json()
    assert st["state"] == "done" and st["result"]["body"].startswith("# Impact analysis")
    assert "Searching the web: q" in st["steps"]
    data = c.get("/api/impact/reports").get_json()
    reports = data["reports"]
    assert reports[0]["project_id"] == "energy-regulation-academy"
    assert reports[0]["impact_score"] == 1 and reports[0]["impact_category"] == "positive"
    assert data["summary"] == {"positive": 1, "undetermined": 0, "negative": 0, "not_rated": 0}
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
        "reports": [], "summary": {"positive": 0, "undetermined": 0, "negative": 0, "not_rated": 0}}


def test_impact_reports_summary_counts_not_rated(tmp_path):
    app, c = client(tmp_path, [])
    impact = tmp_path / "impact"
    impact.mkdir()
    (impact / "old.md").write_text("---\nproject_id: old\ngenerated: '2026-01-01'\n---\n# Old", encoding="utf-8")
    (impact / "neg.md").write_text("---\nproject_id: neg\nimpact_score: -1\nimpact_category: negative\n---\n# N",
                                   encoding="utf-8")
    (impact / "zero.md").write_text("---\nproject_id: zero\nimpact_score: 0\n---\n# Z", encoding="utf-8")
    data = c.get("/api/impact/reports").get_json()
    assert data["summary"] == {"positive": 0, "undetermined": 1, "negative": 1, "not_rated": 1}
    old = next(r for r in data["reports"] if r["project_id"] == "old")
    assert "impact_score" not in old
