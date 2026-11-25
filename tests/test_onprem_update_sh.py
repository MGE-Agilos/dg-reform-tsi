"""deploy/onprem/update.sh with a fake Python: order of the steps, policies, options, exit codes and lock.

Needs bash (Git Bash on Windows); skipped otherwise.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "onprem" / "update.sh"

FAKE_PY = r"""#!/usr/bin/env bash
# Fake python: "python -m tsi_agent --settings S <step> args..." or "python .github/scripts/ci_summary.py ..."
if [ "$1" = "-m" ]; then step="$5"; shift 4; else step="summary"; fi
echo "$*" >> "$FAKE_LOG"
if [ "$step" = run ] && [ -n "${FAKE_SLEEP:-}" ]; then sleep "$FAKE_SLEEP"; fi
for pair in ${FAKE_CODES:-}; do
  if [ "${pair%%=*}" = "$step" ]; then exit "${pair#*=}"; fi
done
exit 0
"""


def _bash() -> str | None:
    for candidate in (os.environ.get("TSI_TEST_BASH"), r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if not candidate or not Path(candidate).exists():
            continue
        try:
            ok = subprocess.run([candidate, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if ok.stdout.strip() == "ok":
            return candidate
    return None


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "deploy" / "onprem").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "deploy" / "onprem" / "update.sh")
    (root / ".github" / "scripts").mkdir(parents=True)
    (root / ".github" / "scripts" / "ci_summary.py").write_text("# stub\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "settings.yaml").write_text("llm_provider: openai   # GPT@EC\nopenai_model: gpt-5.1\n",
                                                   encoding="utf-8")
    (root / ".env").write_text("OPENAI_API_KEY=k\nTSI_DATABASE_URL=postgresql://u:p@h/db\n", encoding="utf-8")
    fake = tmp_path / "fakepy"
    fake.write_bytes(FAKE_PY.encode())
    fake.chmod(0o755)
    return root, fake, tmp_path / "calls.txt"


def run(repo, *args, codes="", month="11", extra_env=None, wait=True):
    root, fake, log = repo
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "TSI_DATABASE_URL")}
    env.update({"TSI_PYTHON": fake.as_posix(), "FAKE_LOG": log.as_posix(), "FAKE_CODES": codes,
                "UPDATE_MONTH": month, "UPDATE_RETRY_WAIT_S": "0", **(extra_env or {})})
    cmd = [BASH, (root / "deploy" / "onprem" / "update.sh").as_posix(), *args]
    if not wait:
        return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)


def steps(repo):
    log = repo[2]
    return [line.split()[0] if line.split() else "" for line in log.read_text().splitlines()] if log.exists() else []


def calls(repo):
    return repo[2].read_text().splitlines()


ALL = ["run", "deliverables", "csr-fetch", "csr-build", "country-reports", "csr-link", "anomalies", "db-sync", ".github/scripts/ci_summary.py"]


def test_bash_syntax():
    assert subprocess.run([BASH, "-n", SCRIPT.as_posix()], capture_output=True).returncode == 0


def test_script_has_lf_line_endings():
    assert b"\r\n" not in SCRIPT.read_bytes()


def test_steps_in_order_and_exit_0(repo):
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert steps(repo) == ALL
    assert "csr-build --fetch-missing" in calls(repo)
    assert "Mode: incremental" in result.stdout
    log = next((repo[0] / "logs").glob("update-*.log")).read_text()
    assert "=== db-sync" in log and "exit code 0" in log
    tsv = next((repo[0] / "logs").glob("update-*/steps.tsv")).read_text().splitlines()
    assert [line.split("\t")[0] for line in tsv] == ALL[:-1]


def test_quota_is_a_warning_and_the_next_steps_run(repo):
    result = run(repo, codes="run=1 csr-link=1")
    assert result.returncode == 1
    assert steps(repo) == ALL
    assert result.stdout.count("LLM quota reached") >= 2


def test_sitemap_unreadable_is_exit_3_and_quota_wins(repo):
    assert run(repo, codes="run=3").returncode == 3
    repo[2].unlink()
    assert run(repo, codes="run=3 deliverables=1").returncode == 1


def test_strict_failure_stops(repo):
    result = run(repo, codes="csr-build=2")
    assert result.returncode == 2
    assert steps(repo) == ["run", "deliverables", "csr-fetch", "csr-build", ".github/scripts/ci_summary.py"]
    assert "STOPPED: csr-build failed" in result.stdout


def test_llm_configuration_error_stops(repo):
    result = run(repo, codes="run=2")
    assert result.returncode == 2 and steps(repo) == ["run", ".github/scripts/ci_summary.py"]


def test_soft_failures_are_warnings(repo):
    result = run(repo, codes="csr-fetch=1 country-reports=1")
    assert result.returncode == 1 and steps(repo) == ALL


def test_db_sync_retried_once_then_warning(repo):
    result = run(repo, codes="db-sync=1")
    assert result.returncode == 1
    assert steps(repo).count("db-sync") == 2
    assert "db-sync failed twice" in result.stdout


def test_options(repo):
    result = run(repo, "--skip-deliverables", "--skip-csr", "--recheck-all", "--no-sync", "--prod-export")
    assert result.returncode == 0
    assert calls(repo)[:3] == ["run --recheck-all", "anomalies", "prod-export"]
    assert "Mode: full check" in result.stdout


def test_detail_only(repo):
    run(repo, "--detail-only")
    assert "csr-link --detail-only" in calls(repo)


def test_scheduled_full_check_in_quarter_months_only(repo):
    run(repo, "--scheduled", month="4")
    assert calls(repo)[0] == "run --recheck-all" and "deliverables --recheck-all" in calls(repo)
    repo[2].unlink()
    result = run(repo, "--scheduled", month="5")
    assert calls(repo)[0] == "run" and "Mode: incremental (scheduled)" in result.stdout


def test_missing_database_url_skips_db_sync_with_warning(repo):
    (repo[0] / ".env").write_text("OPENAI_API_KEY=k\n", encoding="utf-8")
    result = run(repo)
    assert result.returncode == 1 and "db-sync" not in steps(repo)
    assert "TSI_DATABASE_URL missing" in result.stdout


def test_database_url_from_the_environment(repo):
    (repo[0] / ".env").write_text("OPENAI_API_KEY=k\n", encoding="utf-8")
    result = run(repo, extra_env={"TSI_DATABASE_URL": "postgresql://x"})
    assert result.returncode == 0 and "db-sync" in steps(repo)


def test_missing_llm_key_stops_before_any_step(repo):
    (repo[0] / ".env").write_text("TSI_DATABASE_URL=postgresql://x\nOPENAI_API_KEY=\n", encoding="utf-8")
    result = run(repo)
    assert result.returncode == 2 and "key OPENAI_API_KEY is missing" in result.stdout
    assert steps(repo) == [".github/scripts/ci_summary.py"]


def test_env_that_is_a_directory_is_ignored(repo):
    (repo[0] / ".env").unlink()
    (repo[0] / ".env").mkdir()   # volume mounted on a missing file
    result = run(repo, extra_env={"OPENAI_API_KEY": "k", "TSI_DATABASE_URL": "postgresql://x"})
    assert result.returncode == 0, result.stdout


def test_unknown_option(repo):
    result = run(repo, "--nope")
    assert result.returncode == 2 and "unknown option" in result.stderr


def test_second_run_while_one_is_running_exits_75(repo):
    first = run(repo, extra_env={"FAKE_SLEEP": "6"}, wait=False)
    try:
        deadline = time.time() + 30
        while not steps(repo) and time.time() < deadline:   # the first run holds the lock once it calls run
            time.sleep(0.2)
        second = run(repo)
        assert second.returncode == 75 and "another update is running" in second.stderr
    finally:
        first.communicate(timeout=120)
    assert first.returncode == 0
    assert run(repo).returncode == 0   # lock released
