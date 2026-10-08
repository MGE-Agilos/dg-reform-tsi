import json
from pathlib import Path

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
    assert second["written"]["refreshed"] == 0


def test_limit_caps_llm_calls(settings):
    gen = FakeChatGenerator()
    summary = run_extraction(settings, SOURCES, countries=["HR"], limit=3, run_id="r",
                             pipeline_factory=factory(gen, FakeFetcher(default=fake_site)))
    assert summary["llm_calls"] == 3 and summary["remaining_detail"] > 0


def test_limit_counts_pages_sent_to_llm_not_unchanged_ones(settings):
    gen1 = FakeChatGenerator()
    first = run_extraction(settings, SOURCES, countries=["HR"], limit=3, run_id="a",
                           pipeline_factory=factory(gen1, FakeFetcher(default=fake_site)))
    assert len(gen1.calls) == 3

    gen2 = FakeChatGenerator()
    second = run_extraction(settings, SOURCES, countries=["HR"], limit=3, run_id="b",
                            pipeline_factory=factory(gen2, FakeFetcher(default=fake_site)))
    assert len(gen2.calls) == 3
    assert second["llm_pages"] == 3


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
    assert extractor.chat_generator.json_schema["required"] == ["summary"]
    assert set(extractor.chat_generator.json_schema["properties"]) == {"summary", "countries"}
    assert set(reloaded.graph.nodes) == {"listing", "discoverer", "judge", "detector", "detail_fetcher", "extractor",
                                         "writer"}
    judge = reloaded.get_component("judge").chat_generator
    assert judge.chat_generator.json_schema["required"] == ["decisions"] and judge.chat_generator.model == "sonnet"


def test_llm_schema_is_ascii_for_the_windows_command_line():
    from tsi_agent.models import ProjectContent
    json.dumps(ProjectContent.model_json_schema()).encode("ascii")


def test_dry_run_saves_the_discovery_report(settings):
    summary = run_extraction(settings, SOURCES, countries=["HR"], dry_run=True, run_id="d2",
                             pipeline_factory=factory(FakeChatGenerator(), FakeFetcher(default=fake_site)))
    discovery = summary["dry_run"]["discovery"]
    path = discovery.pop("file")
    assert path.endswith("d2.discovery.json")
    report = json.loads(open(path, encoding="utf-8").read())
    assert report["refs"] == discovery["refs"] > 60
    assert discovery == {"refs": report["refs"], "detail": report["detail"], "sitemap_urls": 0,
                         "sitemap_new_urls": 0, "merged_into_thin": 0, "new_projects": 0,
                         "duplicates_merged": len(report["duplicates_merged"]), "not_project_pages": 0,
                         "sitemap_failed": 0, "sitemap_deferred": 0, "judged": 0, "merged_by_judge": 0,
                         "judge_rejected": 0, "judge_llm_calls": 0}
    assert LocalProjectStore(settings.output_dir).all() == {}


def test_dry_run_with_sitemap_calls_the_merge_judge(settings):
    import tsi_agent.components.merge_judge as mj
    from test_merge_judge import ScriptedJudge, decision

    base = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"
    page_url = f"{base}/skills-education-and-training/adultdigitalup-improvement-digital-skills-adults-cyprus_en"
    cy = SOURCES["factsheet_url"].format(slug="cyprus")
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               f"<url><loc>{page_url}</loc></url></urlset>").encode()
    factsheet = ('<details class="ecl-accordion__item"><summary>2021</summary><div class="ecl-accordion__content">'
                 "<ul><li>Improve the digital skills of adults in Cyprus</li></ul></div></details>").encode()
    detail = (b"<html><body><h1>ADULTDIGITALUP - Improvement of the Digital Skills of Adults in Cyprus</h1><main>"
              b"<dl class='ecl-description-list'><dt>Funding Programme</dt><dd>Technical Support Instrument (TSI)</dd>"
              b"<dt>Year</dt><dd>2021</dd></dl><h2>T</h2><p>Support to Cyprus for adult digital skills.</p></main>"
              b"</body></html>")
    fetcher = FakeFetcher(pages={SOURCES["sitemap_url"]: sitemap, cy: factsheet, page_url: detail},
                          cached_pages={page_url: detail})
    gen = ScriptedJudge([[decision("adultdigitalup-improvement-digital-skills-adults-cyprus",
                                   "tsi-2021-improve-the-digital-skills-of-adults-in-cyprus", "high", "reworded")]])

    def make(settings, sources, dry_run=False, model=None):
        pipe = build_pipeline(settings, sources, dry_run=dry_run, judge_generator=gen)
        for name in ("listing", "discoverer", "judge"):
            pipe.get_component(name)._fetcher = fetcher
        return pipe

    summary = run_extraction(settings, SOURCES, dry_run=True, run_id="j1", pipeline_factory=make)
    discovery = summary["dry_run"]["discovery"]
    assert (discovery["judged"], discovery["merged_by_judge"], discovery["new_projects"]) == (1, 1, 0)
    assert len(gen.prompts) == 1
    report = json.loads(open(discovery["file"], encoding="utf-8").read())
    assert report["judge"]["merged_by_judge"][0]["thin_id"] == "tsi-2021-improve-the-digital-skills-of-adults-in-cyprus"
    assert (Path(settings.output_dir) / mj.DECISIONS_FILE).exists()
    assert LocalProjectStore(settings.output_dir).all() == {}
