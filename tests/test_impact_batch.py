import json

import pytest

from tsi_agent import __main__ as cli
from tsi_agent.components.claude_cli import LLMCallError, LLMQuotaExceeded
from tsi_agent.impact.batch import country_iso, exit_code, order_key, run_batch, select_projects
from tsi_agent.impact.catalog import load_project_records


def proj(pid, *, year=2022, countries=("LT",), csrs=(), results=None, policy_area="green-transition",
         programme="TSI"):
    return {"project_id": pid, "title": pid.title(), "year": year, "programme": programme,
            "countries": list(countries), "policy_area": policy_area, "detail_page": True, "results": results,
            "csrs": list(csrs), "summary": ""}


PROJECTS = [
    proj("old-linked-achieved", year=2019, csrs=["csr-LT-2018-1"], results="achieved"),
    proj("new-linked-expected", year=2024, csrs=["csr-LT-2023-1"], results="expected"),
    proj("new-linked-achieved", year=2023, csrs=["csr-LT-2022-1"], results="achieved"),
    proj("unlinked-recent", year=2025, results="achieved"),
    proj("latvia", year=2023, countries=("LV",), csrs=["csr-LV-2022-1"], results="achieved",
         policy_area="digital-transition", programme="SRSP"),
]


def test_country_iso_accepts_code_or_name():
    assert country_iso("lt") == "LT" and country_iso("Lithuania") == "LT" and country_iso(" czech republic ") == "CZ"
    assert country_iso("Atlantis") is None


def test_order_linked_achieved_then_recent():
    ordered, unknown = select_projects(PROJECTS)
    assert [p["project_id"] for p in ordered] == ["latvia", "new-linked-achieved", "old-linked-achieved",
                                                  "new-linked-expected", "unlinked-recent"]
    assert unknown == []
    assert order_key(PROJECTS[2]) < order_key(PROJECTS[1])


def test_filters():
    ids = lambda **kw: [p["project_id"] for p in select_projects(PROJECTS, **kw)[0]]  # noqa: E731
    assert ids(country="LV") == ["latvia"]
    assert ids(policy_area="Digital-Transition") == ["latvia"]
    assert ids(programme="srsp") == ["latvia"]
    assert ids(results="expected") == ["new-linked-expected"]
    assert ids(year_from=2023, year_to=2024, country="LT") == ["new-linked-achieved", "new-linked-expected"]
    assert "unlinked-recent" not in ids(linked_only=True)


def test_ids_keep_given_order_and_report_unknown():
    selected, unknown = select_projects(PROJECTS, ids=["unlinked-recent", "nope", "latvia"], country="LT")
    assert [p["project_id"] for p in selected] == ["unlinked-recent"]  # filtres appliques : latvia ecarte
    assert unknown == ["nope"]


class Runner:
    def __init__(self, errors=None):
        self.calls = []
        self.errors = errors or {}

    def __call__(self, pid, meta):
        self.calls.append((pid, meta))
        if pid in self.errors:
            raise self.errors[pid]
        return {"front": {"impact_score": 6, "impact_level": "moderate"}, "cost_usd": 0.5}


def test_run_batch_skips_existing_reports_and_respects_limit(tmp_path):
    (tmp_path / "a.md").write_text("---\n---\n", encoding="utf-8")
    runner = Runner()
    lines = []
    summary = run_batch([proj("a"), proj("b"), proj("c"), proj("d")], runner, impact_dir=tmp_path, limit=2,
                        log=lines.append)
    assert [c[0] for c in runner.calls] == ["b", "c"]
    assert runner.calls[0][1]["title"] == "B" and "csrs" not in runner.calls[0][1]  # meta = champs du catalogue
    assert summary["done"] == ["b", "c"] and summary["skipped"] == ["a"] and summary["remaining"] == ["d"]
    assert summary["cost_usd"] == 1.0 and exit_code(summary) == 0
    assert any(line.startswith("[1/2] b") for line in lines)


def test_run_batch_redo_reanalyses_existing(tmp_path):
    (tmp_path / "a.md").write_text("---\n---\n", encoding="utf-8")
    runner = Runner()
    summary = run_batch([proj("a")], runner, impact_dir=tmp_path, redo=True, log=lambda s: None)
    assert summary["done"] == ["a"] and summary["skipped"] == []


def test_run_batch_stops_on_quota_wrapped_in_pipeline_error(tmp_path):
    wrapped = RuntimeError("pipeline failed")
    wrapped.__cause__ = LLMQuotaExceeded("usage limit reached")
    runner = Runner(errors={"b": wrapped})
    summary = run_batch([proj("a"), proj("b"), proj("c")], runner, impact_dir=tmp_path, log=lambda s: None)
    assert [c[0] for c in runner.calls] == ["a", "b"]
    assert summary["done"] == ["a"] and summary["quota_exceeded"] and summary["remaining"] == ["b", "c"]
    assert exit_code(summary) == 1


def test_run_batch_failure_continues(tmp_path):
    runner = Runner(errors={"a": LLMCallError("timeout after 600s")})
    summary = run_batch([proj("a"), proj("b")], runner, impact_dir=tmp_path, log=lambda s: None)
    assert summary["done"] == ["b"] and summary["failed"][0]["project_id"] == "a"
    assert "timeout" in summary["failed"][0]["error"] and exit_code(summary) == 0


def test_run_batch_time_budget_stops_between_analyses(tmp_path):
    now = [0.0]

    def runner(pid, meta):
        now[0] += 400  # chaque analyse "dure" 400 s
        return {"front": {}, "cost_usd": 0}
    summary = run_batch([proj("a"), proj("b"), proj("c")], runner, impact_dir=tmp_path, time_budget_s=600,
                        log=lambda s: None, clock=lambda: now[0])
    assert summary["done"] == ["a", "b"] and summary["remaining"] == ["c"] and summary["time_budget_reached"]
    assert exit_code(summary) == 124


def test_run_batch_invalid_id_is_failed_not_run(tmp_path):
    runner = Runner()
    summary = run_batch([proj("Bad Id")], runner, impact_dir=tmp_path, log=lambda s: None)
    assert runner.calls == [] and summary["failed"] == [{"project_id": "Bad Id", "error": "invalid project id"}]


PROJECT_MD = """---
project_id: {pid}
title: {title}
programme: TSI
year: 2023
countries:
- LT
policy_area: green-transition
results: achieved
detail_page: true
{csrs}---

# {title}

Intro.

## Summary (AI-generated)

The project   helped Lithuania.
"""


def write_projects(out, state=True):
    (out / "projects").mkdir(parents=True)
    (out / "projects" / "a.md").write_text(PROJECT_MD.format(pid="a", title="A", csrs="csrs:\n- csr-LT-2022-1\n"),
                                           encoding="utf-8")
    (out / "projects" / "b.md").write_text(PROJECT_MD.format(pid="b", title="B", csrs=""), encoding="utf-8")
    (out / "projects" / "ghost.md").write_text(PROJECT_MD.format(pid="ghost", title="G", csrs=""),
                                               encoding="utf-8")
    if state:
        (out / "state").mkdir()
        (out / "state" / "index.json").write_text(json.dumps({"projects": {"a": {}, "b": {}}}), encoding="utf-8")


def test_load_project_records_uses_state_and_reads_summary(tmp_path):
    write_projects(tmp_path)
    records = load_project_records(tmp_path)
    assert [r["project_id"] for r in records] == ["a", "b"]  # ghost absent de l'etat
    a = records[0]
    assert a["csrs"] == ["csr-LT-2022-1"] and a["results"] == "achieved" and a["countries"] == ["LT"]
    assert a["summary"] == "The project helped Lithuania." and records[1]["csrs"] == []


def _cli_setup(tmp_path):
    out = tmp_path / "output"
    write_projects(out)
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {out.as_posix()}\nimpact_dir: {(out / 'impact').as_posix()}\n"
                        f"logs_dir: {(tmp_path / 'logs').as_posix()}\n", encoding="utf-8")
    return out, settings


def test_cli_impact_batch_unknown_country(tmp_path, capsys):
    _, settings = _cli_setup(tmp_path)
    assert cli.main(["--settings", str(settings), "impact-batch", "--country", "Atlantis"]) == 2
    assert "Atlantis" in capsys.readouterr().err


def test_cli_impact_batch_nothing_to_do_builds_no_pipeline(tmp_path, capsys, monkeypatch):
    out, settings = _cli_setup(tmp_path)
    (out / "impact").mkdir()
    (out / "impact" / "a.md").write_text("---\n---\n", encoding="utf-8")
    import tsi_agent.impact.pipeline as pipeline

    def boom(*a, **k):
        raise AssertionError("pipeline built")
    monkeypatch.setattr(pipeline, "build_impact_pipeline", boom)
    assert cli.main(["--settings", str(settings), "impact-batch", "--linked-only", "--country", "Lithuania"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["skipped"] == ["a"] and summary["done"] == [] and summary["selected"] == 1


def test_cli_impact_batch_runs_analyses_and_stops_on_quota(tmp_path, capsys, monkeypatch):
    _, settings = _cli_setup(tmp_path)
    import tsi_agent.impact.pipeline as pipeline
    import tsi_agent.impact.scoring as scoring
    import tsi_agent.impact.service as service

    class Pipe:
        def warm_up(self):
            pass
    monkeypatch.setattr(pipeline, "build_impact_pipeline", lambda settings: Pipe())
    monkeypatch.setattr(service, "default_report_formatter", lambda settings: "formatter")
    monkeypatch.setattr(scoring, "default_scorer", lambda settings: "scorer")
    monkeypatch.setattr(cli, "_claude_cli_missing", lambda: False)
    calls = []

    def analyse(pipe, pid, **kw):
        calls.append((pid, kw["formatter"], kw["scorer"], kw["project_meta"]["title"]))
        if pid == "b":
            raise LLMQuotaExceeded("limit")
        return {"front": {"impact_score": 7}, "cost_usd": 1.25}
    monkeypatch.setattr(service, "analyse", analyse)
    assert cli.main(["--settings", str(settings), "impact-batch"]) == 1
    assert calls == [("a", "formatter", "scorer", "A"), ("b", "formatter", "scorer", "B")]
    summary = json.loads(capsys.readouterr().out)
    assert summary["done"] == ["a"] and summary["remaining"] == ["b"] and summary["quota_exceeded"]


@pytest.mark.parametrize("error", [RuntimeError("couche vectorisee absente")])
def test_cli_impact_batch_pipeline_unavailable_is_config_error(tmp_path, capsys, monkeypatch, error):
    _, settings = _cli_setup(tmp_path)
    import tsi_agent.impact.pipeline as pipeline

    def fail(settings):
        raise error
    monkeypatch.setattr(pipeline, "build_impact_pipeline", fail)
    monkeypatch.setattr(cli, "_claude_cli_missing", lambda: False)
    assert cli.main(["--settings", str(settings), "impact-batch"]) == 2
    assert "couche vectorisee absente" in capsys.readouterr().err
