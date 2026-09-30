import pytest

from tsi_agent import __main__ as cli


def test_unknown_country_code_is_rejected(capsys):
    with pytest.raises(SystemExit) as err:
        cli.main(["run", "--countries", "FR,XX"])
    assert err.value.code == 2 and "XX" in capsys.readouterr().err


def test_missing_sources_file_is_a_clean_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as err:
        cli.main(["--sources", str(tmp_path / "nope.yaml"), "run", "--dry-run"])
    assert err.value.code == 2 and "introuvable" in capsys.readouterr().err


def test_export_yaml(tmp_path):
    out = tmp_path / "pipeline.yaml"
    assert cli.main(["export-yaml", str(out)]) == 0
    assert "ClaudeCliChatGenerator" in out.read_text(encoding="utf-8")


def test_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: {"quota_exceeded": True})
    assert cli.main(["run", "--dry-run"]) == 1
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: {"quota_exceeded": False})
    assert cli.main(["run", "--dry-run"]) == 0

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(cli, "run_extraction", boom)
    assert cli.main(["run", "--dry-run"]) == 2
    assert "kaput" in capsys.readouterr().err
