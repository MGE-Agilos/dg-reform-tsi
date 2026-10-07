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
