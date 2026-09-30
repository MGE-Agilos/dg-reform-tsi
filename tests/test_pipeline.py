import json

import pytest

from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from tsi_agent.components import ClaudeCliChatGenerator, ProjectExtractor
from tsi_agent.config import Settings, load_sources
from tsi_agent.pipeline import build_pipeline, load_pipeline
from tsi_agent.runner import run_extraction
from tsi_agent.store import LocalProjectStore

SOURCES = load_sources("config/sources.yaml")
HR = SOURCES["factsheet_url"].format(slug="croatia")


def fake_site(url: str) -> bytes | None:
    if url == HR:
        return fixture_bytes("factsheet_croatia.html")
    if url == SOURCES["spotlight_url"]:
        return fixture_bytes("spotlight.html")
    if url.endswith("/green-transition_en"):
        return fixture_bytes("policy_area_green_transition.html")
    if "/technical-support-instrument-0/" in url and url.endswith("_en") and "tsi-country-factsheets" not in url:
        if url.count("/") <= 4:  # autres pages de domaine politique : vides
            return b"<html><main></main></html>"
        return fixture_bytes("detail_hems_croatia.html")
    if url == SOURCES["programme_docs"][0]["url"]:
        return fixture_bytes("tsi_performance.html")
    return None


@pytest.fixture
def settings(tmp_path):
    return Settings(output_dir=str(tmp_path / "output"), logs_dir=str(tmp_path / "logs"),
                    cache_dir=str(tmp_path / "cache"), min_interval_s=0, chunk_size=20)


def factory(generator, fetcher):
    def make(settings, sources, dry_run=False, model=None):
        pipe = build_pipeline(settings, sources, dry_run=dry_run, chat_generator=generator)
        for name in ("listing", "discoverer", "detail_fetcher"):
            if name in pipe.graph.nodes:
                pipe.get_component(name)._fetcher = fetcher
        return pipe
    return make


def test_two_runs_first_writes_second_is_incremental(settings):
    gen, fetcher = FakeChatGenerator(), FakeFetcher(default=fake_site)
    first = run_extraction(settings, SOURCES, countries=["HR"], run_id="run1", pipeline_factory=factory(gen, fetcher))
    store = LocalProjectStore(settings.output_dir)
    projects = store.all()
    n_detail = sum(e.detail_page for e in projects.values())
    assert n_detail > 40 and len(projects) > 60
    assert first["llm_calls"] == len(gen.calls) == n_detail
    assert first["chunks"] >= 3 and first["quota_exceeded"] is False
    assert "Programme document tsi-performance has no content (0 chars)" in " ".join(first["alerts"]) or \
        any("tsi-performance" in a for a in first["alerts"])
    assert (settings_path := (store.root / "programme" / "country-croatia.md")).exists()
    summary_file = json.loads((store.root.parent / "logs" / "runs" / "run1.summary.json").read_text(encoding="utf-8"))
    assert summary_file["llm_calls"] == n_detail

    gen2 = FakeChatGenerator()
    second = run_extraction(settings, SOURCES, countries=["HR"], run_id="run2",
                            pipeline_factory=factory(gen2, FakeFetcher(default=fake_site)))
    assert second["llm_calls"] == 0 and gen2.calls == []
    assert second["written"]["thin"] == 0 and second["written"]["detail"] == 0


def test_limit_caps_llm_calls(settings):
    gen = FakeChatGenerator()
    summary = run_extraction(settings, SOURCES, countries=["HR"], limit=3, run_id="r",
                             pipeline_factory=factory(gen, FakeFetcher(default=fake_site)))
    assert summary["llm_calls"] == 3 and summary["remaining_detail"] > 0


def test_dry_run_writes_nothing(settings):
    summary = run_extraction(settings, SOURCES, countries=["HR"], dry_run=True, run_id="d",
                             pipeline_factory=factory(FakeChatGenerator(), FakeFetcher(default=fake_site)))
    assert summary["dry_run"]["detail_candidates"] > 40 and summary["llm_calls"] == 0
    assert LocalProjectStore(settings.output_dir).all() == {}


def test_yaml_roundtrip_for_deepset(settings):
    pipe = build_pipeline(settings, SOURCES)
    yaml_text = pipe.dumps()
    assert "tsi_agent.components.claude_cli.ClaudeCliChatGenerator" in yaml_text
    reloaded = load_pipeline(yaml_text)
    extractor = reloaded.get_component("extractor")
    assert isinstance(extractor, ProjectExtractor)
    assert isinstance(extractor.chat_generator, ClaudeCliChatGenerator)
    assert extractor.chat_generator.json_schema["required"] == ["summary", "context", "support_delivered",
                                                                "results_achieved"]
    assert set(reloaded.graph.nodes) == {"listing", "discoverer", "detector", "detail_fetcher", "extractor", "writer"}


def test_llm_schema_is_ascii_for_the_windows_command_line():
    from tsi_agent.models import ProjectContent
    json.dumps(ProjectContent.model_json_schema()).encode("ascii")
