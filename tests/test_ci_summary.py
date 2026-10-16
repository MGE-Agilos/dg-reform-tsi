"""Resume Markdown du workflow GitHub Actions (.github/scripts/ci_summary.py) et validite du workflow."""

import importlib.util
import json
from pathlib import Path

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
        "skipped_by_sitemap": 820})
    write(runs / "20261101-040000.summary.json", {
        **base, "run_id": "20261101-040000", "started_at": "2026-11-01T04:00:00+00:00", "duration_s": 600.0,
        "llm_calls": 4, "cost_usd": 0.3, "mode": "deliverables", "quota_exceeded": True,
        "written": {"publications": 4, "projects_updated": 3, "orphans_removed": 0},
        "remaining_publications": 9, "failed": ["p1"]})
    write(runs / "20261101-050000.summary.json", {
        **base, "run_id": "20261101-050000", "started_at": "2026-11-01T05:00:00+00:00", "duration_s": 300.0,
        "llm_calls": 5, "cost_usd": 0.2, "mode": "csr_link", "projects": 900, "linked": 5, "cached": 880,
        "deferred": 15, "no_candidates": 0, "failed": [], "quota_exceeded": False})
    # Run d'une execution precedente : hors du resume grace a --since.
    write(runs / "20261001-030000.summary.json", {
        **base, "run_id": "20261001-030000", "started_at": "2026-10-01T03:00:00+00:00", "duration_s": 1.0,
        "llm_calls": 99, "cost_usd": 99.0, "mode": "chat"})
    write(runs / "broken.summary.json", "{not json")
    write(work / "steps.tsv", "seed\t0\t12\nrun\t0\t1801\ndeliverables\t1\t600\ncsr-fetch\t0\t30\n"
                              "csr-build\t0\t5\ncsr-link\t0\t300\nanomalies\t0\t2\nsupabase-sync\t0\t20\n"
                              "prod-export\t0\t40\nrelease-data\t0\t60\n")
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
    assert "| deliverables | quota (exit 1) | 10 min |" in md
    assert "| run | ok | 30 min |" in md
    # Runs de cette execution seulement, dans l'ordre.
    assert "20261001-030000" not in md and "99.00" not in md
    lines = [line for line in md.splitlines() if line.startswith("| 202611")]
    assert [line.split(" | ")[1] for line in lines] == ["seed", "run", "deliverables", "csr-link"]
    run_line = lines[1]
    assert "| 8 |" in run_line and "| 820 |" in run_line and "6 detail, 2 title-only, 3 refreshed, 1 delisted" \
        in run_line and "$0.42" in run_line
    assert "830 of 835 detail pages dated" in lines[0]
    assert "4 publications" in lines[2] and "9 remaining" in lines[2] and "1 failed" in lines[2] \
        and "quota" in lines[2]
    assert "5 newly linked, 880 cached, 15 deferred" in lines[3]
    assert "**Total LLM cost: $0.92**" in md
    assert "CSR files: 2140" in md and "project_csr_links 3100" in md
    assert "5400 files, 41000 chunks" in md and "project 2200 files / 9000 chunks" in md
    assert "[data-2026-11-01](https://github.com/o/r/releases/tag/data-2026-11-01)" in md


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
    assert wf["permissions"] == {"contents": "write"}
    job = wf["jobs"]["update"]
    assert job["timeout-minutes"] == 340 and job["runs-on"] == "ubuntu-latest"
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
