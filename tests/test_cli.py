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


def test_index_command_and_exit_codes(monkeypatch, capsys):
    calls = {}

    def fake(settings, *, full=False, **kw):
        calls["full"] = full
        return {"status": "ok"}
    monkeypatch.setattr(cli, "run_index", fake)
    assert cli.main(["index", "--full"]) == 0 and calls["full"] is True

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(cli, "run_index", boom)
    assert cli.main(["index"]) == 2
    assert "kaput" in capsys.readouterr().err


def test_export_index_yaml(tmp_path, monkeypatch):
    from haystack.components.embedders import MockDocumentEmbedder
    monkeypatch.setattr("tsi_agent.index_pipeline.default_embedder", lambda s: MockDocumentEmbedder(model="m"))
    out = tmp_path / "index.yaml"
    assert cli.main(["export-yaml", "--index", str(out)]) == 0
    assert "VectorLayerWriter" in out.read_text(encoding="utf-8")


def test_export_index_yaml_without_local_embedder_is_a_clean_error(tmp_path, monkeypatch, capsys):
    def boom(settings):
        raise RuntimeError('embedder local absent : installer avec pip install -e ".[local]"')
    monkeypatch.setattr("tsi_agent.index_pipeline.default_embedder", boom)
    out = tmp_path / "index.yaml"
    assert cli.main(["export-yaml", "--index", str(out)]) == 2
    assert "embedder local absent" in capsys.readouterr().err
    assert not out.exists()


def test_chat_command_starts_server(monkeypatch):
    started = {}

    class FakeApp:
        def run(self, host, port, threaded):
            started.update(host=host, port=port)
    monkeypatch.setattr("tsi_agent.chat.web.create_app", lambda settings, **kw: FakeApp())
    monkeypatch.setattr("webbrowser.open", lambda url: started.setdefault("url", url))
    assert cli.main(["chat", "--port", "9999", "--no-browser"]) == 0
    assert started == {"host": "127.0.0.1", "port": 9999}


def test_chat_command_without_vector_layer(monkeypatch, capsys):
    from tsi_agent.vector_state import VectorLayerError

    def boom(settings, **kw):
        raise VectorLayerError("couche vectorisee absente : lancez d'abord python -m tsi_agent index")
    monkeypatch.setattr("tsi_agent.chat.web.create_app", boom)
    assert cli.main(["chat", "--no-browser"]) == 2
    assert "tsi_agent index" in capsys.readouterr().err


def test_export_chat_yaml(tmp_path, monkeypatch):
    from haystack import Pipeline
    monkeypatch.setattr("tsi_agent.chat.pipeline.build_chat_pipeline", lambda settings, areas: Pipeline())
    out = tmp_path / "chat.yaml"
    assert cli.main(["export-yaml", "--chat", str(out)]) == 0 and out.exists()


def test_deliverables_command_exit_codes(monkeypatch, capsys):
    seen = {}

    def fake(settings, **kw):
        seen.update(kw)
        return {"quota_exceeded": False}
    monkeypatch.setattr(cli, "run_deliverables", fake)
    assert cli.main(["deliverables", "--full", "--limit", "3"]) == 0 and seen["full"] and seen["limit"] == 3
    monkeypatch.setattr(cli, "run_deliverables", lambda *a, **k: {"quota_exceeded": True})
    assert cli.main(["deliverables"]) == 1

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(cli, "run_deliverables", boom)
    assert cli.main(["deliverables"]) == 2 and "kaput" in capsys.readouterr().err


def test_export_deliverables_yaml(tmp_path):
    out = tmp_path / "d.yaml"
    assert cli.main(["export-yaml", "--deliverables", str(out)]) == 0
    assert "DeliverableSummarizer" in out.read_text(encoding="utf-8")


def test_deliverables_refresh_metadata_needs_no_llm(monkeypatch, capsys):
    monkeypatch.setattr(cli, "refresh_deliverables_metadata", lambda settings: {"files": 3, "updated": 1})

    def no_run(*a, **k):
        raise AssertionError("run_deliverables must not be called")
    monkeypatch.setattr(cli, "run_deliverables", no_run)
    assert cli.main(["deliverables", "--refresh-metadata"]) == 0
    assert '"updated": 1' in capsys.readouterr().out


def _rescore_setup(tmp_path, monkeypatch, replies):
    import json as _json

    from test_chat_agent import ScriptedGenerator
    from test_impact_report import META, REPORT, RUN, SOURCES
    from tsi_agent.impact import scoring
    from tsi_agent.impact.report import render_report, write_report
    impact = tmp_path / "impact"
    for pid in ("aaa", "bbb"):
        if not (impact / f"{pid}.md").exists():
            write_report(impact, pid, render_report(dict(META, project_id=pid), REPORT, SOURCES, RUN))
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"impact_dir: {impact.as_posix()}\nlogs_dir: {(tmp_path / 'logs').as_posix()}\n",
                        encoding="utf-8")
    scorer = ScriptedGenerator([r if isinstance(r, Exception) else _json.dumps(r) for r in replies])
    monkeypatch.setattr(scoring, "default_scorer", lambda s: scorer)
    return impact, settings, scorer


def test_impact_rescore_all_reports(tmp_path, monkeypatch, capsys):
    from tsi_agent.render import parse_front_matter
    impact, settings, scorer = _rescore_setup(tmp_path, monkeypatch, [
        {"impact_score": 7, "impact_rationale": "Good [1]."}, {"impact_score": 2, "impact_rationale": "Weak."}])
    assert cli.main(["--settings", str(settings), "impact-rescore"]) == 0
    out = capsys.readouterr().out
    assert "aaa 7/10 strong" in out and "bbb 2/10 weak" in out and "Coût total" in out
    front = parse_front_matter((impact / "aaa.md").read_text(encoding="utf-8"))[0]
    assert front["impact_score"] == 7 and front["impact_level"] == "strong"
    assert not scorer.replies and not scorer.received[0][1].text.startswith("---")
    assert list((tmp_path / "logs" / "runs").glob("*.summary.json"))


def test_impact_rescore_selected_and_errors(tmp_path, monkeypatch, capsys):
    from tsi_agent.components.claude_cli import LLMCallError
    impact, settings, _ = _rescore_setup(tmp_path, monkeypatch, [{"impact_score": 5, "impact_rationale": "x"}])
    assert cli.main(["--settings", str(settings), "impact-rescore", "bbb"]) == 0
    assert "bbb 5/10 moderate" in capsys.readouterr().out
    assert "impact_score" not in (impact / "aaa.md").read_text(encoding="utf-8")
    assert cli.main(["--settings", str(settings), "impact-rescore", "nope"]) == 2
    assert "nope" in capsys.readouterr().err
    _rescore_setup(tmp_path, monkeypatch, [LLMCallError("timeout")])
    assert cli.main(["--settings", str(settings), "impact-rescore", "aaa"]) == 2
    assert "timeout" in capsys.readouterr().err
    assert "impact_score" not in (impact / "aaa.md").read_text(encoding="utf-8")
