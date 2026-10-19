"""Resume Markdown du workflow GitHub Actions (.github/scripts/ci_summary.py) et validite du workflow."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "ci_summary.py"
WORKFLOW = ROOT / ".github" / "workflows" / "update-data.yml"


def load():
    spec = importlib.util.spec_from_file_location("ci_summary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def fake_logs(tmp_path: Path) -> tuple[Path, Path, Path]:
    runs, work = tmp_path / "logs" / "runs", tmp_path / "ci-work"
    base = {"counts": {}, "alerts": [], "tokens": {}, "status": "ok"}
    write(runs / "20261101-030500.summary.json", {
        **base, "run_id": "20261101-030500", "started_at": "2026-11-01T03:05:00+00:00", "duration_s": 12.0,
        "llm_calls": 0, "cost_usd": 0.0, "mode": "seed_sitemap_lastmod", "detail_known": 835, "seeded": 830,
        "without_date": 5, "sitemap_dates": 4999})
    write(runs / "20261101-031000.summary.json", {
        **base, "run_id": "20261101-031000", "started_at": "2026-11-01T03:10:00+00:00", "duration_s": 1800.5,
        "llm_calls": 7, "cost_usd": 0.42, "chunks": 1, "quota_exceeded": False, "llm_pages": 6,
        "written": {"detail": 6, "thin": 2, "refreshed": 3, "delisted": 1, "programme_docs": 0, "write_failed": 0},
        "skipped_by_sitemap": 820, "detail_without_sitemap_date": 5, "failed": 2, "fetch_failed": 1,
        "extract_failed": 1})
    write(runs / "20261101-040000.summary.json", {
        **base, "run_id": "20261101-040000", "started_at": "2026-11-01T04:00:00+00:00", "duration_s": 600.0,
        "llm_calls": 4, "cost_usd": 0.3, "mode": "deliverables", "quota_exceeded": True,
        "written": {"publications": 4, "projects_updated": 3, "orphans_removed": 0},
        "remaining_publications": 9, "failed": ["p1"], "skipped_by_sitemap": 500,
        "publication_without_sitemap_date": 3})
    write(runs / "20261101-050000.summary.json", {
        **base, "run_id": "20261101-050000", "started_at": "2026-11-01T05:00:00+00:00", "duration_s": 300.0,
        "llm_calls": 5, "cost_usd": 0.2, "mode": "csr_link", "projects": 900, "linked": 5, "cached": 880,
        "deferred": 15, "no_candidates": 0, "failed": [], "quota_exceeded": False})
    # Run d'une execution precedente : hors du resume grace a --since.
    write(runs / "20261001-030000.summary.json", {
        **base, "run_id": "20261001-030000", "started_at": "2026-10-01T03:00:00+00:00", "duration_s": 1.0,
        "llm_calls": 99, "cost_usd": 99.0, "mode": "chat"})
    write(runs / "broken.summary.json", "{not json")
    write(work / "steps.tsv", "seed\t0\t12\nrun\t0\t1801\tquota\ndeliverables\t1\t600\tquota\n"
                              "csr-fetch\t0\t30\tsoft\ncsr-build\t0\t5\tstrict\ncsr-link\t124\t3600\tquota\n"
                              "anomalies\t0\t2\tstrict\nsupabase-sync\t1\t20\tsoft\n"
                              "prod-export\t0\t40\tstrict\nrelease-data\t1\t60\tstrict\n")
    write(work / "csr-build.out", json.dumps({"rows": 5000, "csr_files": 2140, "dir": "output/csr"}, indent=2))
    write(work / "supabase-sync.out", json.dumps({"projects": 2200, "csrs": 2140, "csr_parts": 6000,
                                                   "project_csr_links": 3100, "anomalies": 40}, indent=2))
    manifest = tmp_path / "manifest.json"
    write(manifest, {"corpus": "tsi", "files": 5400, "chunks": 41000,
                     "files_by_type": {"project": 2200, "csr": 2140}, "chunks_by_type": {"project": 9000, "csr": 8000}})
    return runs, work, manifest


def test_summary_tables(tmp_path):
    ci = load()
    runs, work, manifest = fake_logs(tmp_path)
    md = ci.render(runs_dir=runs, work_dir=work, manifest=manifest, since="2026-11-01T03:00:00+00:00",
                   mode="incremental", state_tag="data-2026-10-01", release_tag="data-2026-11-01",
                   release_url="https://github.com/o/r/releases/tag/data-2026-11-01")
    assert "Mode: **incremental**" in md and "`data-2026-10-01`" in md
    # Etapes : quota signale.
    assert "| deliverables | quota (exit 1), continued | 10 min |" in md
    assert "| run | ok | 30 min |" in md
    assert "| seed | ok | 12 s |" in md  # ancienne ligne sans politique
    assert "| csr-link | time budget reached, continued | 60 min |" in md
    assert "| supabase-sync | failed (exit 1), continued | 20 s |" in md
    assert "| release-data | failed (exit 1) | 1 min |" in md  # exit 1 d'une etape stricte : pas un quota
    assert "Supabase tables may be partial" in md and "publish_release=false" in md
    # Runs de cette execution seulement, dans l'ordre.
    assert "20261001-030000" not in md and "99.00" not in md
    lines = [line for line in md.splitlines() if line.startswith("| 202611")]
    assert [line.split(" | ")[1] for line in lines] == ["seed", "run", "deliverables", "csr-link"]
    run_line = lines[1]
    assert "| 8 |" in run_line and "| 820 |" in run_line and "6 detail, 2 title-only, 3 refreshed, 1 delisted" \
        in run_line and "$0.42" in run_line
    assert "2 failed (1 fetch, 1 extraction)" in run_line and "5 without sitemap date" in run_line
    assert "830 of 835 detail pages dated" in lines[0]
    assert "4 publications" in lines[2] and "9 remaining" in lines[2] and "1 failed" in lines[2] \
        and "quota" in lines[2]
    assert "| 500 |" in lines[2] and "3 without sitemap date" in lines[2]
    assert "5 newly linked, 880 cached, 15 deferred" in lines[3]
    assert "**Total LLM cost: $0.92**" in md
    assert "CSR files: 2140" in md and "project_csr_links 3100" in md
    assert "5400 files, 41000 chunks" in md and "project 2200 files / 9000 chunks" in md
    assert "[data-2026-11-01](https://github.com/o/r/releases/tag/data-2026-11-01)" in md
    assert "partial" not in md.split("### Release")[1].lower()


def test_summary_shows_the_notes_of_the_workflow(tmp_path):
    ci = load()
    md = ci.render(runs_dir=tmp_path, work_dir=tmp_path, manifest=tmp_path / "m.json",
                   notes=["deliverables: budget raised to 150 min"])
    assert "> deliverables: budget raised to 150 min" in md
    assert ci.main(["--runs-dir", str(tmp_path), "--manifest", str(tmp_path / "m.json"), "--note", "a", "--note",
                    "", "--note", "b"]) == 0


def test_summary_points_to_the_partial_state_artifact(tmp_path):
    ci = load()
    md = ci.render(runs_dir=tmp_path, work_dir=tmp_path, manifest=tmp_path / "m.json",
                   partial_artifact="output-state-partial-42")
    assert "output-state-partial-42" in md and "restore" in md.lower()


def test_summary_never_fails_on_missing_inputs(tmp_path, capsys):
    ci = load()
    md = ci.render(runs_dir=tmp_path / "nope", work_dir=tmp_path / "nope2", manifest=tmp_path / "m.json")
    assert "No run summary found" in md and "not published" in md.lower()
    assert ci.main(["--runs-dir", str(tmp_path / "nope"), "--manifest", str(tmp_path / "m.json")]) == 0
    assert "## TSI data update" in capsys.readouterr().out


def test_workflow_is_valid_yaml_with_expected_triggers_and_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    on = wf[True] if True in wf else wf["on"]  # PyYAML lit la cle `on` comme un booleen
    assert on["schedule"] == [{"cron": "0 3 1 * *"}]
    inputs = on["workflow_dispatch"]["inputs"]
    assert {k: v["default"] for k, v in inputs.items()} == {
        "full": False, "recheck_all": False, "detail_only": True, "skip_deliverables": False, "publish_release": True,
        "sync_supabase": True}
    assert wf["permissions"] == {"contents": "write", "actions": "write"}
    job = wf["jobs"]["update"]
    assert job["timeout-minutes"] == 350 and job["runs-on"] == "ubuntu-latest"
    steps = job["steps"]
    # CLI epingle, version en tete du workflow.
    assert wf["env"]["CLAUDE_CODE_VERSION"].count(".") == 2
    assert any("@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in s.get("run", "") for s in steps)
    ping = next(s["run"] for s in steps if "claude -p" in s.get("run", ""))
    assert '--tools ""' in ping and "--json-schema" in ping and "structured_output" in ping
    checkout = next(s for s in steps if s.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["persist-credentials"] is False
    node = next(s for s in steps if s.get("uses") == "actions/setup-node@v4")
    assert node["with"]["node-version"] == "22"
    assert "unzip -o" in text
    # Etat sans dates : verification complete, plus d'initialisation automatique.
    assert "--seed-sitemap-lastmod" not in text and "--recheck-all" in text
    # Budgets des etapes LLM : leur somme laisse au moins 30 min aux etapes de publication.
    env = wf["env"]
    assert int(env["LLM_BUDGET_TOTAL_MIN"]) + 15 + 30 <= job["timeout-minutes"] <= 350
    for key in ("RUN_BUDGET_MIN", "RUN_RECHECK_BUDGET_MIN", "DELIVERABLES_BUDGET_MIN",
                "DELIVERABLES_FIRST_BUDGET_MIN", "CSR_LINK_BUDGET_MIN"):
        assert 0 < int(env[key]) <= int(env["LLM_BUDGET_TOTAL_MIN"])
    plan = next(s for s in steps if s.get("id") == "plan")
    for name in ("Projects (run)", "Publications (deliverables)", "Project -> CSR links (csr-link)"):
        step = next(s for s in steps if s.get("name") == name)
        assert "steps.plan.outputs" in str(step["timeout-minutes"]) and "steps.plan.outputs" in json.dumps(step)
    assert plan["name"]
    # Travail paye conserve en cas d'echec ou d'annulation.
    partial = [s for s in steps if s.get("if") == "failure() || cancelled()"]
    assert any("write_state_zip" in s.get("run", "") for s in partial)
    upload = next(s for s in partial if s.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"]["retention-days"] == 30
    # Supabase : une nouvelle tentative apres 60 s.
    sync = next(s for s in steps if "supabase-sync" in s.get("run", ""))
    assert sync["env"]["STEP_RETRY_AFTER_S"] == "60"
    # Maintien du planning (desactive apres 60 jours sans activite).
    keep = next(s for s in steps if "workflows/update-data.yml/enable" in s.get("run", ""))
    assert str(keep["if"]).startswith("always()") and keep["continue-on-error"] is True
    assert wf["concurrency"]["group"] == "update-data"
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    for action in ("actions/checkout@v4", "actions/setup-python@v5", "actions/setup-node@v4",
                   "actions/upload-artifact@v4"):
        assert action in uses
    # Secrets : seulement dans `env:` des etapes, jamais dans une commande.
    for step in job["steps"]:
        assert "secrets." not in str(step.get("run", "")), step.get("name")
    secret_steps = {s["name"] for s in job["steps"] if "secrets." in json.dumps(s.get("env", {}))}
    assert secret_steps and "secrets." not in json.dumps(job.get("env", {})) and "secrets." not in \
        json.dumps(wf.get("env", {}))
    for name in ("ANTHROPIC_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        assert f"secrets.{name}" in text
    always = {s["name"] for s in job["steps"] if str(s.get("if", "")).startswith("always()")}
    assert any("summary" in n.lower() for n in always) and any("log" in n.lower() for n in always)


def run_plan(tmp_path, **env):
    """Script du step `plan` du workflow, execute avec bash (budgets et mode du run)."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash absent")
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["update"]["steps"] if s.get("id") == "plan")
    script = tmp_path / "plan.sh"
    script.write_text(step["run"], encoding="utf-8", newline="\n")
    out = tmp_path / "out.txt"
    full_env = {**{k: str(v) for k, v in wf["env"].items()}, "GITHUB_OUTPUT": out.as_posix(),
                "PATH": __import__("os").environ["PATH"], "FULL": "false", "RECHECK": "false",
                "HAS_SITEMAP_DATES": "true", "HAS_PUBLICATION_DATES": "true",
                "HAS_PUBLICATION_LINKS": "true", **env}
    subprocess.run([bash, script.as_posix()], env=full_env, check=True, capture_output=True)
    return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)


def test_plan_incremental_month(tmp_path):
    plan = run_plan(tmp_path)
    assert (plan["run_args"], plan["run_label"], plan["run_budget"]) == ("", "incremental", "60")
    assert (plan["deliverables_budget"], plan["csr_link_budget"], plan["note"]) == ("75", "60", "")
    assert plan["run_note"] == ""
    assert plan["run_timeout"] == "70" and plan["deliverables_timeout"] == "85" and plan["csr_link_timeout"] == "70"


def test_plan_first_run_without_publication_dates_gets_a_larger_deliverables_budget(tmp_path):
    plan = run_plan(tmp_path, HAS_PUBLICATION_DATES="false")
    assert plan["deliverables_budget"] == "150" and "150 min" in plan["note"]
    assert int(plan["run_budget"]) + 150 + int(plan["csr_link_budget"]) <= 300


def test_plan_budgets_stay_within_the_total_for_a_full_check(tmp_path):
    for env in ({"RECHECK": "true"}, {"HAS_SITEMAP_DATES": "false"}, {"FULL": "true"}):
        plan = run_plan(tmp_path, HAS_PUBLICATION_DATES="false", **env)
        assert plan["run_budget"] == "150"
        total = int(plan["run_budget"]) + int(plan["deliverables_budget"]) + int(plan["csr_link_budget"])
        assert total <= 300 and int(plan["deliverables_budget"]) == 90
    assert run_plan(tmp_path, RECHECK="true")["run_args"] == "--recheck-all"
    assert run_plan(tmp_path, HAS_SITEMAP_DATES="false")["run_label"] == "full check"
    assert run_plan(tmp_path, FULL="true")["run_args"] == "--full"


def test_plan_state_without_publication_links_rereads_every_detail_page(tmp_path):
    plan = run_plan(tmp_path, HAS_PUBLICATION_LINKS="false")
    assert (plan["run_args"], plan["run_label"], plan["run_budget"]) == ("--recheck-all", "full check", "150")
    assert "publication links" in plan["run_note"]
    both = run_plan(tmp_path, HAS_PUBLICATION_LINKS="false", HAS_PUBLICATION_DATES="false")
    assert int(both["run_budget"]) + int(both["deliverables_budget"]) + int(both["csr_link_budget"]) <= 300
    assert run_plan(tmp_path, HAS_PUBLICATION_LINKS="false", FULL="true")["run_args"] == "--full"
