"""Pipeline Haystack de l'etage livrables (export YAML pour deepset)."""

from typing import Any

from haystack import Pipeline

from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.components.deliverables import (DeliverableSummarizer, DeliverableTextExtractor, DeliverableWriter,
                                               PublicationDiscoverer, PublicationFetcher)
from tsi_agent.config import Settings
from tsi_agent.models import DeliverableContent


def default_deliverables_generator(settings: Settings, model: str | None = None) -> Any:
    return ClaudeCliChatGenerator(model=model or settings.model, timeout_s=settings.deliverables_llm_timeout_s,
                                  json_schema=DeliverableContent.model_json_schema())


def build_deliverables_pipeline(settings: Settings, *, chat_generator: Any = None, model: str | None = None) -> Pipeline:
    http = {"cache_dir": settings.cache_dir, "min_interval_s": settings.min_interval_s,
            "user_agent": settings.user_agent}
    p = Pipeline()
    p.add_component("discoverer", PublicationDiscoverer(output_dir=settings.output_dir, **http))
    p.add_component("publications", PublicationFetcher(output_dir=settings.output_dir,
                                                        recheck_days=settings.deliverables_recheck_days, **http))
    p.add_component("texts", DeliverableTextExtractor(output_dir=settings.output_dir,
                                                      deliverables_dir=settings.deliverables_dir,
                                                      max_pdf_mb=settings.deliverables_max_pdf_mb, **http))
    p.add_component("summarizer", DeliverableSummarizer(
        chat_generator=chat_generator or default_deliverables_generator(settings, model),
        max_concurrency=settings.max_concurrency, pricing=settings.pricing,
        max_words=settings.deliverables_max_words))
    p.add_component("writer", DeliverableWriter(output_dir=settings.output_dir))
    p.connect("discoverer.publications", "publications.publications")
    p.connect("publications.to_process", "texts.publications")
    p.connect("texts.texts", "summarizer.texts")
    p.connect("summarizer.records", "writer.records")
    p.connect("publications.unchanged", "writer.unchanged")
    p.connect("discoverer.publications", "writer.discovered")
    p.connect("discoverer.complete", "writer.complete")
    return p
