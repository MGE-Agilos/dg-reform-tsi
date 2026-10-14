import json

import pytest
import yaml
from conftest import CONTENT_JSON, FakeChatGenerator, FakeFetcher, fixture_bytes

from tsi_agent import llm_check
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings

URL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/x/{}_en"


def project_file(out, pid, title, *, detail=True, summary="Old summary of the project.", sections=2):
    fm = {"project_id": pid, "title": title, "programme": "TSI", "year": 2022, "countries": ["HR"],
          "results": "expected" if detail else None, "detail_page": detail,
          "source_urls": [URL.format(pid)] if detail else ["https://example.org/factsheet"],
          "extraction": {"run_id": "r1", "model": "claude-cli/claude-sonnet-4-6"}}
    body = [f"# {title}", "Intro.", f"## Summary (AI-generated)\n\n{summary}"]
    body += [f"## Section {i}\n\nText {i}." for i in range(sections)]
    body += ["## Supported CSRs (AI-assessed)\n\n- csr-HR-2022-1.1", "## Deliverables (AI-generated summary)\n\n### Report",
             f"## Sources\n\n- {URL.format(pid)}"]
    text = f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n" + "\n\n".join(body) + "\n"
    path = out / "projects" / f"{pid}.md"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    files = [project_file(out, "hems-croatia", "HEMS in Croatia"),
             project_file(out, "bim-greece", "BIM in Greece"),
             project_file(out, "not-cached", "Not cached"),
             project_file(out, "thin-one", "Thin project", detail=False)]
    fetcher = FakeFetcher(cached_pages={URL.format("hems-croatia"): fixture_bytes("detail_hems_croatia.html"),
                                        URL.format("bim-greece"): fixture_bytes("detail_bim_greece.html"),
                                        "https://example.org/factsheet": b"<html></html>"})
    settings = Settings(output_dir=str(out), logs_dir=str(tmp_path / "logs"), cache_dir=str(tmp_path / "cache"))
    return settings, out, files, fetcher


def snapshot(out):
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in (out / "projects").rglob("*") if p.is_file()}


def test_check_compares_cached_projects_and_writes_only_the_report(setup):
    settings, out, files, fetcher = setup
    before = snapshot(out)
    gen = FakeChatGenerator()
    result = llm_check.run_check(settings, n=3, generator=gen, fetcher=fetcher)
    assert snapshot(out) == before
    assert sorted(p["project_id"] for p in result["projects"]) == ["bim-greece", "hems-croatia"]
    assert len(gen.calls) == 2 and fetcher.calls == []  # aucun appel reseau
    report = (out / "reports" / "llm_check.md").read_text(encoding="utf-8")
    assert result["report"] == str(out / "reports" / "llm_check.md")
    for pid, title in (("hems-croatia", "HEMS in Croatia"), ("bim-greece", "BIM in Greece")):
        assert f"## {title}" in report and URL.format(pid) in report
    assert "Old summary of the project." in report
    assert json.loads(CONTENT_JSON)["summary"] in report
    assert "| Sections |" in report and "| Results |" in report and "| Countries |" in report
    assert "| Summary length (words) |" in report
    assert "claude-cli/claude-sonnet-4-6" in report and "fake/model" in report
    assert "0.0200" in report  # cout total (2 appels a 0,01 $)
    assert "not-cached" not in report and "Thin project" not in report


def test_existing_view_counts_official_sections_only(setup):
    settings, out, files, _ = setup
    view = llm_check.existing_view(files[0].read_text(encoding="utf-8"))
    assert view["sections"] == 2 and view["summary"] == "Old summary of the project."
    assert view["results"] == "expected" and view["countries"] == ["HR"]


def test_selection_is_deterministic_and_limited(setup):
    settings, out, files, fetcher = setup
    first = llm_check.select_projects(out, fetcher, n=1)
    assert len(first) == 1
    assert [p["project_id"] for p in llm_check.select_projects(out, fetcher, n=1)] == [first[0]["project_id"]]
    assert {p["project_id"] for p in llm_check.select_projects(out, fetcher, n=10)} == {"hems-croatia", "bim-greece"}


def test_no_cached_page_is_an_error(setup):
    settings, out, files, _ = setup
    with pytest.raises(llm_check.LLMCheckError, match="cache"):
        llm_check.run_check(settings, n=3, generator=FakeChatGenerator(), fetcher=FakeFetcher())
    assert not (out / "reports" / "llm_check.md").exists()


def test_quota_stops_and_is_reported(setup):
    settings, out, files, fetcher = setup
    gen = FakeChatGenerator(error=LLMQuotaExceeded("429 quota"))
    result = llm_check.run_check(settings, n=3, generator=gen, fetcher=fetcher)
    assert result["quota_exceeded"] is True and len(gen.calls) == 1
    report = (out / "reports" / "llm_check.md").read_text(encoding="utf-8")
    assert "429 quota" in report


def test_cli_llm_check_without_cache_returns_1(tmp_path, capsys):
    import tsi_agent.__main__ as cli

    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    project_file(out, "hems-croatia", "HEMS in Croatia")
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(f"output_dir: {out.as_posix()}\nlogs_dir: {(tmp_path / 'logs').as_posix()}\n"
                             f"cache_dir: {(tmp_path / 'cache').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings_file), "llm-check", "--n", "2"]) == 1
    assert "cache" in capsys.readouterr().err


def test_cli_llm_check_passes_the_settings_and_n(setup, monkeypatch, tmp_path, capsys):
    import tsi_agent.__main__ as cli

    settings, out, files, fetcher = setup
    seen = {}

    def fake_run_check(s, *, n, generator=None, fetcher=None, seed=llm_check.SEED):
        seen.update(n=n, provider=s.llm_provider)
        return {"report": "r.md", "projects": [{"project_id": "x", "error": None}], "cost_usd": 0.0,
                "quota_exceeded": False}

    monkeypatch.setattr(llm_check, "run_check", fake_run_check)
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(f"output_dir: {out.as_posix()}\nllm_provider: openai\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings_file), "llm-check", "--n", "5"]) == 0
    assert seen == {"n": 5, "provider": "openai"}
    assert "r.md" in capsys.readouterr().out
