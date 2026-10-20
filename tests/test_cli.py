import json

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


def test_discover_only_is_the_dry_run_and_prints_the_discovery(monkeypatch, capsys, tmp_path):
    seen = {}
    report_file = tmp_path / "r.discovery.json"
    report = {"refs": 2140, "detail": 860, "sitemap": {"urls": 670, "new_urls": 190, "fetched": 188,
                                                       "not_project_pages": ["u"], "failed": [], "deferred": []},
              "merged_into_thin": [{"sitemap_slug": f"slug-{i}", "thin_id": f"srsp-2019-thin-{i}", "score": 0.9,
                                    "rule": "fuzzy", "title": "T", "programme": "SRSP", "year": 2019}
                                   for i in range(35)],
              "new_projects": [{"project_id": "new-one", "title": "New", "programme": "TSI", "year": 2024,
                                "url": "https://x/new-one_en"}],
              "duplicates_merged": [{"detail_id": "d", "thin_id": "t", "score": 1.0}],
              "judge": {"judged": 60, "llm_calls": 6, "errors": 0,
                        "merged_by_judge": [{"sitemap_slug": "adultdigitalup", "thin_id": "tsi-2021-improve",
                                             "confidence": "high", "reason": "reworded", "cached": False}],
                        "rejected": [{"sitemap_slug": "x"}, {"sitemap_slug": "y"}]}}
    report_file.write_text(json.dumps(report), encoding="utf-8")

    def fake(settings, sources, **kw):
        seen.update(kw)
        return {"quota_exceeded": False, "dry_run": {"discovery": {"file": str(report_file)}}}

    monkeypatch.setattr(cli, "run_extraction", fake)
    assert cli.main(["run", "--discover-only"]) == 0
    assert seen["dry_run"] is True
    out = capsys.readouterr().out
    assert "Projects after merge: 2140" in out
    assert "Sitemap project URLs: 670 (190 not in the listings)" in out
    assert "Merged into thin projects: 35" in out
    assert "slug-0 -> srsp-2019-thin-0 (0.900, fuzzy)" in out
    assert "slug-29 -> " in out and "slug-30 -> " not in out and "... 5 more" in out
    assert "New projects: 1" in out and "new-one" in out
    assert "Duplicates merged (thin + detail page): 1" in out
    assert "Judged by LLM (no rule merge, candidates found): 60 (6 LLM calls, 0 errors)" in out
    assert "Merged by the LLM judge: 1" in out and "adultdigitalup -> tsi-2021-improve (high: reworded)" in out
    assert "Rejected by the LLM judge: 2" in out
    assert str(report_file) in out


def test_anomalies_command_writes_report(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "projects" / "a.md").write_text(
        "---\nproject_id: a\ntitle: A\nprogramme: TSI\nyear: 2020\ndetail_page: false\n---\n\n# A\n",
        encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {out.as_posix()}\n", encoding="utf-8")
    reports = tmp_path / "reports"
    code = cli.main(["--settings", str(settings), "anomalies", "--out", str(reports), "--year", "2026"])
    assert code == 0
    assert (reports / "anomalies.csv").exists() and (reports / "anomalies.md").exists()


def test_csr_build_command(tmp_path):
    import openpyxl
    from tsi_agent.csr import COLUMNS

    out = tmp_path / "output"
    (out / "csr").mkdir(parents=True)
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    wb.active.append(["France", "2023", "1", "2024 - Semester Cycle (June)", "Do X.", "Healthcare", "-", "No",
                      "Annual", "Some Progress"])
    wb.save(out / "csr" / "csr_full.xlsx")
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {out.as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-build"]) == 0
    assert (out / "csr" / "csr-FR-2023-1.md").exists()


def test_csr_build_without_export_is_a_clean_error(tmp_path, capsys):
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-build"]) == 1
    assert "csr-fetch" in capsys.readouterr().err


def test_csr_fetch_failure_is_a_clean_error(tmp_path, capsys, monkeypatch):
    from tsi_agent import csr_fetch

    def boom(out):
        raise RuntimeError("Qlik: no qlik-csrf-token header")

    monkeypatch.setattr(csr_fetch, "fetch", boom)
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-fetch"]) == 1
    assert "csr-fetch a échoué" in capsys.readouterr().err


def test_csr_link_without_export_is_a_clean_error(tmp_path, capsys):
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-link"]) == 2  # 1 = quota seulement
    assert "csr-fetch" in capsys.readouterr().err


def test_csr_link_unexpected_error_is_exit_code_2(tmp_path, capsys, monkeypatch):
    import tsi_agent.csr as csr
    import tsi_agent.csr_link as csr_link
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    source = tmp_path / "output" / "csr" / "csr_full.xlsx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x")
    monkeypatch.setattr(cli, "_check_llm", lambda s: True)
    monkeypatch.setattr(csr, "read_rows", lambda path: [])
    monkeypatch.setattr(csr, "build_csrs", lambda rows, *a: [])

    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(csr_link, "run_linking", boom)
    assert cli.main(["--settings", str(settings), "csr-link"]) == 2
    assert "kaput" in capsys.readouterr().err


def test_supabase_sync_without_credentials_is_a_clean_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    sources = tmp_path / "sources.yaml"
    sources.write_text("{}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "--sources", str(sources), "supabase-sync"]) == 1
    assert "SUPABASE_SERVICE_KEY" in capsys.readouterr().err


def _csr_build_setup(tmp_path):
    import openpyxl
    from tsi_agent.csr import COLUMNS

    out = tmp_path / "output"
    (out / "csr").mkdir(parents=True)
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    wb.active.append(["France", "2025", "1", "2025 - Semester Cycle (June)", "Do X.", "Healthcare", "-", "No",
                      "Annual", "Some Progress"])
    wb.save(out / "csr" / "csr_full.xlsx")
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {out.as_posix()}\ncache_dir: {(tmp_path / 'cache').as_posix()}\n",
                        encoding="utf-8")
    return out, settings


COMMISSION_TEXT = """# CSR 2026

## Commission Recommendation - Croatia

Source: https://ec.example/HR.pdf

--- page 1 ---
HEREBY RECOMMENDS that Croatia take action in 2026 and 2027 to:
1. Adhere to the net expenditure path.
2. Ensure continuity of reforms.
Done at Brussels,

## Commission Recommendation - Malta

Source: https://ec.example/MT.pdf

--- page 1 ---
Recitals only.
"""


def test_csr_build_includes_commission_recommendations_and_documents(tmp_path, capsys):
    from tsi_agent.csr_commission import PUBLICATION_ID
    out, settings = _csr_build_setup(tmp_path)
    (out / "deliverables").mkdir()
    (out / "deliverables" / f"{PUBLICATION_ID}.md").write_text(COMMISSION_TEXT, encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-build"]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["csr_files"] == 3 and summary["csr_documents"] == 2
    assert summary["commission"]["by_country"] == {"HR": 2, "MT": 0}
    assert "MT: no 'HEREBY RECOMMENDS ... to:' part" in captured.err and "--fetch-missing" in captured.err
    names = sorted(p.name for p in (out / "csr").glob("*.md"))
    assert names == ["csr-FR-2025-1.md", "csr-HR-2026-1.md", "csr-HR-2026-2.md", "csrdoc-FR-2025.md",
                     "csrdoc-HR-2026.md"]
    assert "stage: Commission recommendation" in (out / "csr" / "csr-HR-2026-1.md").read_text(encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-build", "--skip-commission"]) == 0
    assert "commission" not in json.loads(capsys.readouterr().out)
    assert sorted(p.name for p in (out / "csr").glob("*.md")) == ["csr-FR-2025-1.md", "csrdoc-FR-2025.md"]


def test_csr_build_fetch_missing_completes_the_local_copy(tmp_path, capsys, monkeypatch):
    from tsi_agent import csr_commission, http
    out, settings = _csr_build_setup(tmp_path)
    seen = {}

    def fake_fetch(fetcher, countries, **kw):
        seen["countries"] = countries
        return COMMISSION_TEXT.split("\n\n", 1)[1]

    monkeypatch.setattr(http, "shared_fetcher", lambda *a: object())
    monkeypatch.setattr(csr_commission, "fetch_missing", fake_fetch)
    assert cli.main(["--settings", str(settings), "csr-build", "--fetch-missing"]) == 0
    assert len(seen["countries"]) == 27
    kept = out / "csr" / "publications" / f"{csr_commission.PUBLICATION_ID}.md"
    assert "Commission Recommendation - Croatia" in kept.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["commission"]["recommendations"] == 2
    assert not list((out / "csr" / "publications").glob("*.tmp"))

    def boom(fetcher, countries, **kw):
        raise http.FetchError("https://x", "HTTP 503", 503)

    monkeypatch.setattr(csr_commission, "fetch_missing", boom)
    assert cli.main(["--settings", str(settings), "csr-build", "--fetch-missing"]) == 1
    assert "téléchargement des PDF CSR impossible" in capsys.readouterr().err
