"""Assemblage du pipeline Haystack (et export YAML pour deepset)."""

from typing import Any

from haystack import Pipeline

from tsi_agent.components import (ChangeDetector, ClaudeCliChatGenerator, DetailPageFetcher, MarkdownProjectWriter,
                                  ProjectDiscoverer, ProjectExtractor, SourceListingFetcher)
from tsi_agent.components.merge_judge import SitemapMergeJudge
from tsi_agent.config import Settings
from tsi_agent.models import ProjectContent

ALLOWED_MODULES = ["tsi_agent"]


def default_chat_generator(settings: Settings, model: str | None = None) -> Any:
    return ClaudeCliChatGenerator(model=model or settings.model, timeout_s=settings.llm_timeout_s,
                                  json_schema=ProjectContent.model_json_schema())


def build_pipeline(settings: Settings, sources: dict[str, Any], *, dry_run: bool = False,
                   chat_generator: Any = None, model: str | None = None, judge_generator: Any = None) -> Pipeline:
    """`judge_generator` : arbitre LLM des pages du sitemap sans fusion (defaut : CLI claude, sonnet)."""
    http = {"cache_dir": settings.cache_dir, "min_interval_s": settings.min_interval_s,
            "user_agent": settings.user_agent}
    p = Pipeline()
    p.add_component("listing", SourceListingFetcher(sources=sources, **http))
    p.add_component("discoverer", ProjectDiscoverer(sitemap_excluded_areas=sources.get("sitemap_excluded_areas"),
                                                    **http))
    p.add_component("judge", SitemapMergeJudge(chat_generator=judge_generator, output_dir=settings.output_dir,
                                               **http))
    p.add_component("detector", ChangeDetector(output_dir=settings.output_dir))
    p.connect("listing.pages", "discoverer.pages")
    p.connect("discoverer.refs", "judge.refs")
    p.connect("discoverer.report", "judge.report")
    p.connect("discoverer.excluded_thin_ids", "judge.excluded_thin_ids")
    p.connect("judge.refs", "detector.refs")
    p.connect("discoverer.scope", "detector.scope")
    p.connect("discoverer.suspect_countries", "detector.suspect_countries")
    p.connect("discoverer.deferred_ids", "detector.deferred_ids")
    if dry_run:
        return p
    p.add_component("detail_fetcher", DetailPageFetcher(output_dir=settings.output_dir, **http))
    p.add_component("extractor", ProjectExtractor(
        chat_generator=chat_generator or default_chat_generator(settings, model),
        max_concurrency=settings.max_concurrency, pricing=settings.pricing))
    p.add_component("writer", MarkdownProjectWriter(output_dir=settings.output_dir))
    p.connect("detector.detail", "detail_fetcher.refs")
    p.connect("detail_fetcher.pages", "extractor.pages")
    p.connect("extractor.records", "writer.records")
    p.connect("detector.thin", "writer.thin")
    p.connect("detail_fetcher.unchanged", "writer.unchanged")
    p.connect("detector.delisted", "writer.delisted")
    p.connect("discoverer.programme_docs", "writer.programme_docs")
    return p


def load_pipeline(yaml_text: str) -> Pipeline:
    return Pipeline.loads(yaml_text, allowed_modules=ALLOWED_MODULES)
