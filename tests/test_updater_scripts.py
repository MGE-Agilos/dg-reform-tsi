"""Scripts PowerShell de la mise a jour sur PC (deploy/updater) : ASCII, syntaxe PS 5.1, tache planifiee (-WhatIf)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

UPDATER = Path(__file__).resolve().parent.parent / "deploy" / "updater"
SCRIPTS = sorted(UPDATER.glob("*.ps1"))
POWERSHELL = shutil.which("powershell.exe")  # Windows PowerShell 5.1
needs_ps = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell absent")


def ps(command: str) -> subprocess.CompletedProcess:
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                           command], capture_output=True, text=True, timeout=120)


def test_scripts_exist_and_are_ascii():
    names = {p.name for p in SCRIPTS}
    assert {"update.ps1", "register-task.ps1", "unregister-task.ps1", "restore-state.ps1"} <= names
    for path in SCRIPTS:
        path.read_bytes().decode("ascii")  # PS 5.1 lit un script sans BOM en ANSI : ASCII seulement


@needs_ps
def test_scripts_parse_with_windows_powershell_5_1():
    for path in SCRIPTS:
        out = ps(f"$t = $null; $e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                 f"'{path}', [ref]$t, [ref]$e) | Out-Null; $PSVersionTable.PSVersion.Major; $e.Count")
        major, errors = out.stdout.split()[:2]
        assert (major, errors) == ("5", "0"), (path.name, out.stdout, out.stderr)


def register_whatif(*args: str) -> str:
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                          str(UPDATER / "register-task.ps1"), "-WhatIf", *args],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


@needs_ps
def test_register_task_whatif_prints_the_default_monthly_task_without_registering():
    text = register_whatif()
    assert "WhatIf: nothing registered" in text
    assert "<Day>1</Day>" in text and re.search(r"<StartBoundary>\d{4}-\d\d-\d\dT03:00:00</StartBoundary>", text)
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in text
    assert "<LogonType>InteractiveToken</LogonType>" in text and "<Password>" not in text
    assert "<December />" in text and "<January />" in text
    root = str(UPDATER.parent.parent)
    assert f"<WorkingDirectory>{root}</WorkingDirectory>" in text
    args = re.search(r"<Arguments>(.*)</Arguments>", text).group(1)
    assert args == (f"-NoProfile -ExecutionPolicy Bypass -File &quot;{UPDATER / 'update.ps1'}&quot; "
                    "-Scheduled -Sync -Publish")


@needs_ps
def test_register_task_whatif_options():
    text = register_whatif("-Day", "15", "-Time", "05:30", "-NoPublish", "-NoSync", "-Index")
    assert "<Day>15</Day>" in text and "T05:30:00</StartBoundary>" in text
    args = re.search(r"<Arguments>(.*)</Arguments>", text).group(1)
    assert args.endswith("-Scheduled -Index") and "-Publish" not in args and "-Sync" not in args


@needs_ps
@pytest.mark.parametrize("args", [("-Day", "31"), ("-Time", "3h")])
def test_register_task_rejects_invalid_options(args):
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                          str(UPDATER / "register-task.ps1"), "-WhatIf", *args],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode != 0 and "<Task" not in out.stdout


def test_update_script_options_and_exit_code_policy():
    text = (UPDATER / "update.ps1").read_text(encoding="ascii")
    for switch in ("RecheckAll", "Sync", "Publish", "Index", "Scheduled", "SkipDeliverables", "SkipCsr", "DetailOnly"):
        assert f"[switch]${switch}" in text
    assert "Start-Transcript" in text and "Stop-Transcript" in text and "ci_summary.py" in text
    assert "@(1, 4, 7, 10) -contains (Get-Date).Month" in text  # verification complete trimestrielle planifiee
    assert "Get-Command claude" in text and "Test-KeyAvailable $keyName" in text
    assert '"data-" + (Get-Date -Format "yyyy-MM-dd")' in text and "gh release view" in text
    assert "exit $script:worst" in text
