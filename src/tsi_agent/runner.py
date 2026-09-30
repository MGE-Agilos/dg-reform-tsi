"""Exécution par lots : le pipeline est relancé jusqu'à épuisement des projets à traiter."""

from typing import Any, Callable

from haystack import Pipeline

from tsi_agent import runlog
from tsi_agent.config import Settings
from tsi_agent.index_pipeline import build_index_pipeline
from tsi_agent.pipeline import build_pipeline

PipelineFactory = Callable[..., Pipeline]


def run_extraction(settings: Settings, sources: dict[str, Any], *, full: bool = False,
                   countries: list[str] | None = None, limit: int | None = None, chunk_size: int | None = None,
                   dry_run: bool = False, model: str | None = None, run_id: str | None = None,
                   pipeline_factory: PipelineFactory = build_pipeline) -> dict[str, Any]:
    chunk = chunk_size if chunk_size is not None else settings.chunk_size
    with runlog.open_run(settings.logs_dir, run_id) as run:
        pipeline = pipeline_factory(settings, sources, dry_run=dry_run, model=model)
        budget = limit
        chunks = 0
        quota = False
        written: dict[str, int] = {}
        llm_pages = 0
        while True:
            chunk_limit = None if dry_run else (chunk if budget is None else min(chunk, budget))
            data: dict[str, Any] = {
                "listing": {"countries": countries},
                "detector": {"run_id": run.run_id, "full": full, "limit": chunk_limit},
            }
            if not dry_run:
                data["detail_fetcher"] = {"full": full}
                data["writer"] = {"run_id": run.run_id}
            out = pipeline.run(data, include_outputs_from={"detector", "detail_fetcher", "extractor", "writer"})
            chunks += 1
            det = out["detector"]
            if dry_run:
                run.extra["dry_run"] = {"detail_candidates": len(det["detail"]), "thin_to_write": len(det["thin"]),
                                        "delisted": len(det["delisted"])}
                break
            for key, value in out.get("writer", {}).get("stats", {}).items():
                written[key] = written.get(key, 0) + value
            processed = len(det["detail"])
            sent = len(out["detail_fetcher"]["pages"])
            llm_pages += sent
            if budget is not None:
                budget -= sent
            quota = bool(out.get("extractor", {}).get("quota_exceeded"))
            if quota or det["remaining"] == 0 or processed == 0 or (budget is not None and budget <= 0):
                run.extra["remaining_detail"] = det["remaining"]
                break
        run.extra.update({"chunks": chunks, "quota_exceeded": quota, "written": written, "llm_pages": llm_pages})
    return run.final_summary


def run_index(settings: Settings, *, full: bool = False, embedder: Any = None, run_id: str | None = None,
              pipeline_factory: PipelineFactory = build_index_pipeline) -> dict[str, Any]:
    """Vectorise les fichiers extraits nouveaux ou modifies (tout, au premier run ou avec full)."""
    with runlog.open_run(settings.logs_dir, run_id) as run:
        pipeline = pipeline_factory(settings, embedder=embedder)
        out = pipeline.run({"loader": {"full": full}},
                           include_outputs_from={"loader", "chunker", "vector_writer"})
        run.extra.update({"mode": "index", "full": out["loader"]["full"],
                          "stats": out["vector_writer"]["stats"], "skipped": out["chunker"]["skipped"]})
    return run.final_summary
