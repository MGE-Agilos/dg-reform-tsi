import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from test_impact_pipeline import impact_pipeline
from test_impact_report import REPORT
from test_impact_tools import FakeEngine, FakeFetcher
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.impact.catalog import load_catalog, search_catalog
from tsi_agent.impact.service import AnalysisFailed, ImpactJobs, JobRunning, analyse, progress_text

FIX = Path(__file__).parent / "fixtures"
META = {"project_id": "energy-regulation-academy", "title": "Energy Regulation Academy", "countries": [],
        "year": 2023, "programme": "TSI", "policy_area": "green-transition"}


def projects_dir(tmp_path):
    d = tmp_path / "out" / "projects"
    d.mkdir(parents=True)
    shutil.copy(FIX / "vec_detail_sections.md", d / "energy-regulation-academy.md")
    shutil.copy(FIX / "vec_detail_page_text.md", d / "building-capacity-competition-authority-spain.md")
    return d


def test_catalog_search_by_title_country_and_year(tmp_path):
    catalog = load_catalog(projects_dir(tmp_path))
    assert len(catalog) == 2
    assert search_catalog(catalog, "energy academy")[0]["project_id"] == "energy-regulation-academy"
    # l'academie couvre aussi l'Espagne (ES) : "spain" seul retient les deux, "spain competition" un seul
    assert {p["project_id"] for p in search_catalog(catalog, "spain")} == {
        "energy-regulation-academy", "building-capacity-competition-authority-spain"}
    assert [p["project_id"] for p in search_catalog(catalog, "spain competition")] == [
        "building-capacity-competition-authority-spain"]
    assert search_catalog(catalog, "")[:1] and search_catalog(catalog, "zzz") == []


def test_progress_text():
    assert progress_text({"action": "tool_call", "target": "web_search", "query": "q1"}) == "Searching the web: q1"
    assert progress_text({"action": "tool_call", "target": "fetch_page", "url": "https://www.oecd.org/x"}) == \
        "Reading: www.oecd.org"
    assert progress_text({"action": "tool_call", "target": "get_project", "doc_id": "p"}) == "Reading project p"
    assert progress_text({"action": "llm_call", "component": "ChatAgent"}) == "Analysing…"
    assert progress_text({"action": "llm_call", "component": "ClaudeWebSearch"}) is None


def run_analysis(tmp_path, final):
    from test_chat_agent import chat_settings
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    pipe = impact_pipeline(tmp_path, [[("get_project", {"doc_id": "energy-regulation-academy"})],
                                      [("web_search", {"query": "q"})], final], settings=s,
                           search_engine=FakeEngine([("News", "https://n.eu", "x")]), page_fetcher=FakeFetcher())
    steps = []
    return analyse(pipe, "energy-regulation-academy", settings=s, project_meta=META, on_step=steps.append), steps, s


def test_analyse_writes_report_and_reports_progress(tmp_path):
    report = dict(REPORT, positive_evidence="Good [2] and invented [8].")
    result, steps, s = run_analysis(tmp_path, json.dumps(report))
    assert "Reading project energy-regulation-academy" in steps and "Searching the web: q" in steps
    text = (tmp_path / "impact" / "energy-regulation-academy.md").read_text(encoding="utf-8")
    assert "Good [2] and invented ." in text and "- [2] News — https://n.eu" in text
    assert 8 in result["front"]["removed_citations"] and result["run_id"]  # sources reelles : [1] et [2]
    assert result["body"].startswith("# Impact analysis — Energy Regulation Academy")


def test_analyse_fails_without_json_or_on_step_limit(tmp_path):
    with pytest.raises(AnalysisFailed):
        run_analysis(tmp_path, "I could not finish.")
    assert not (tmp_path / "impact" / "energy-regulation-academy.md").exists()


def test_jobs_single_run_status_and_errors():
    gate = threading.Event()

    def runner(project_id, on_step):
        on_step("step one")
        gate.wait(5)
        if project_id == "quota":
            raise LLMQuotaExceeded("limit")
        return {"project_id": project_id, "body": "# R", "front": {}}

    jobs = ImpactJobs(runner)
    jobs.start("p1")
    with pytest.raises(JobRunning):
        jobs.start("p2")
    for _ in range(100):  # le thread peut ne pas avoir encore emis son etape
        if jobs.status()["steps"]:
            break
        time.sleep(0.05)
    assert jobs.status()["state"] == "running" and jobs.status()["steps"] == ["step one"]
    gate.set()
    jobs.wait(5)
    assert jobs.status()["state"] == "done" and jobs.status()["result"]["body"] == "# R"
    jobs.start("quota")
    jobs.wait(5)
    st = jobs.status()
    assert st["state"] == "error" and "limit" in st["error"].lower()


def test_progress_text_shows_failures():
    assert progress_text({"action": "tool_call", "target": "web_search", "result": "error", "query": "q"}) == \
        "Web search failed: q"
    assert progress_text({"action": "tool_call", "target": "web_search", "result": "limit", "query": "q"}) == \
        "Search limit reached"
    assert progress_text({"action": "tool_call", "target": "fetch_page", "result": "error",
                          "url": "https://www.oecd.org/x"}) == "Could not read: www.oecd.org"
    assert progress_text({"action": "tool_call", "target": "get_project", "result": "not_found",
                          "doc_id": "p"}) == "Project not found: p"
    assert progress_text({"action": "tool_call", "target": "web_search", "result": "ok", "query": "q"}) == \
        "Searching the web: q"


def test_quota_in_web_search_stops_the_analysis(tmp_path):
    from test_chat_agent import chat_settings
    from tsi_agent.impact.service import QUOTA_MESSAGE
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    pipe = impact_pipeline(tmp_path, [[("web_search", {"query": "q"})], json.dumps(REPORT)], settings=s,
                           search_engine=FakeEngine(error=LLMQuotaExceeded("limit")), page_fetcher=FakeFetcher())
    jobs = ImpactJobs(lambda pid, on_step: analyse(pipe, pid, settings=s, project_meta=META, on_step=on_step))
    jobs.start("energy-regulation-academy")
    jobs.wait(30)
    st = jobs.status()
    assert st["state"] == "error" and st["error"] == QUOTA_MESSAGE
    assert not (tmp_path / "impact" / "energy-regulation-academy.md").exists()


def _summary(s, run_id):
    return json.loads((Path(s.logs_dir) / "runs" / f"{run_id}.summary.json").read_text(encoding="utf-8"))


def test_analyse_logs_removed_citations_alert_in_the_run(tmp_path):
    report = dict(REPORT, positive_evidence="Good [2] and invented [8].")
    result, _, s = run_analysis(tmp_path, json.dumps(report))
    summary = _summary(s, result["run_id"])
    assert any("8" in a for a in summary["alerts"]) and summary["status"] == "ok"
    assert set(result) == {"project_id", "front", "body", "run_id", "cost_usd", "duration_s"}


def test_analyse_write_failure_marks_run_as_error(tmp_path, monkeypatch):
    import tsi_agent.impact.service as service

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(service, "write_report", boom)
    with pytest.raises(OSError):
        run_analysis(tmp_path, json.dumps(REPORT))
    summaries = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "logs" / "runs").glob("*.summary.json")]
    impact = [x for x in summaries if x.get("mode") == "impact"]
    assert len(impact) == 1 and impact[0]["status"] == "error" and "disk full" in impact[0]["error"]


def test_invalid_final_json_is_repaired_by_the_formatter(tmp_path):
    from test_chat_agent import ScriptedGenerator, chat_settings
    from tsi_agent.impact.report import REPORT_SCHEMA
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    broken = '{"project_overview": "The minister said "eHealth is a priority" [1]."}'
    pipe = impact_pipeline(tmp_path, [[("get_project", {"doc_id": "energy-regulation-academy"})], broken],
                           settings=s, search_engine=FakeEngine(), page_fetcher=FakeFetcher())
    formatter = ScriptedGenerator([json.dumps(dict(REPORT, project_overview="The minister said 'eHealth' [1]."))])
    result = analyse(pipe, "energy-regulation-academy", settings=s, project_meta=META, formatter=formatter)
    assert "The minister said 'eHealth' [1]." in result["body"]
    assert broken in formatter.received[0][1].text
    assert "verdict" in json.dumps(REPORT_SCHEMA)
    events = (tmp_path / "logs" / "runs" / f"{result['run_id']}.jsonl").read_text(encoding="utf-8")
    assert '"bad_format"' in events and '"repaired"' in events
    # formateur qui echoue aussi : AnalysisFailed, pas de rapport
    pipe = impact_pipeline(tmp_path, [[("get_project", {"doc_id": "energy-regulation-academy"})], broken],
                           settings=s, search_engine=FakeEngine(), page_fetcher=FakeFetcher())
    (tmp_path / "impact" / "energy-regulation-academy.md").unlink()
    with pytest.raises(AnalysisFailed):
        analyse(pipe, "energy-regulation-academy", settings=s, project_meta=META,
                formatter=ScriptedGenerator(["still not json"]))
    assert not (tmp_path / "impact" / "energy-regulation-academy.md").exists()
