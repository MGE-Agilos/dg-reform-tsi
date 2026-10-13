"""Exécution par lots : le pipeline est relancé jusqu'à épuisement des projets à traiter."""

import json
from typing import Any, Callable

from pathlib import Path

from haystack import Pipeline

from tsi_agent import runlog
from tsi_agent.components.deliverables import META_FIELDS, project_meta
from tsi_agent.config import Settings
from tsi_agent.deliverables_state import DeliverableStore
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
from tsi_agent.index_pipeline import build_index_pipeline
from tsi_agent.pipeline import build_pipeline
from tsi_agent.render import parse_front_matter, render
from tsi_agent.store import _retry_on_lock, shared_store

PipelineFactory = Callable[..., Pipeline]
MAX_PAIRS_SHOWN = 30


def save_discovery_report(logs_dir: str, run_id: str, report: dict[str, Any]) -> dict[str, Any]:
    """Ecrit le bilan complet de la decouverte dans `<logs_dir>/runs/<run_id>.discovery.json` ; renvoie les compteurs."""
    path = Path(logs_dir) / "runs" / f"{run_id}.discovery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    sitemap = report.get("sitemap", {})
    judge = report.get("judge", {})
    return {"refs": report.get("refs", 0), "detail": report.get("detail", 0),
            "sitemap_urls": sitemap.get("urls", 0), "sitemap_new_urls": sitemap.get("new_urls", 0),
            "merged_into_thin": len(report.get("merged_into_thin", [])),
            "new_projects": len(report.get("new_projects", [])),
            "duplicates_merged": len(report.get("duplicates_merged", [])),
            "not_project_pages": len(sitemap.get("not_project_pages", [])),
            "sitemap_failed": len(sitemap.get("failed", [])), "sitemap_deferred": len(sitemap.get("deferred", [])),
            "judged": judge.get("judged", 0), "merged_by_judge": len(judge.get("merged_by_judge", [])),
            "judge_rejected": len(judge.get("rejected", [])), "judge_llm_calls": judge.get("llm_calls", 0),
            "file": str(path)}


def format_discovery(report: dict[str, Any], path: str, max_pairs: int = MAX_PAIRS_SHOWN) -> str:
    """Bilan lisible de la decouverte (--dry-run), pour relire les fusions avant l'extraction reelle."""
    sitemap = report.get("sitemap", {})
    merged = report.get("merged_into_thin", [])
    new = report.get("new_projects", [])
    dups = report.get("duplicates_merged", [])
    lines = [f"Projects after merge: {report.get('refs', 0)} ({report.get('detail', 0)} with a detail page)",
             f"Sitemap project URLs: {sitemap.get('urls', 0)} ({sitemap.get('new_urls', 0)} not in the listings)",
             f"  not project pages: {len(sitemap.get('not_project_pages', []))}, failed: "
             f"{len(sitemap.get('failed', []))}, deferred: {len(sitemap.get('deferred', []))}",
             f"Merged into thin projects: {len(merged)}"]
    lines += [f"  {m['sitemap_slug']} -> {m['thin_id']} ({m['score']:.3f}, {m['rule']})" for m in merged[:max_pairs]]
    if len(merged) > max_pairs:
        lines.append(f"  ... {len(merged) - max_pairs} more")
    judge = report.get("judge", {})
    by_judge = judge.get("merged_by_judge", [])
    lines.append(f"Judged by LLM (no rule merge, candidates found): {judge.get('judged', 0)} "
                 f"({judge.get('llm_calls', 0)} LLM calls, {judge.get('errors', 0)} errors)")
    lines.append(f"Merged by the LLM judge: {len(by_judge)}")
    lines += [f"  {m['sitemap_slug']} -> {m['thin_id']} ({m['confidence']}: {m['reason']})"
              for m in by_judge[:max_pairs]]
    if len(by_judge) > max_pairs:
        lines.append(f"  ... {len(by_judge) - max_pairs} more")
    lines.append(f"Rejected by the LLM judge: {len(judge.get('rejected', []))}")
    lines.append(f"New projects: {len(new)}")
    lines += [f"  {p['project_id']} ({p.get('programme')} {p.get('year')})" for p in new[:max_pairs]]
    if len(new) > max_pairs:
        lines.append(f"  ... {len(new) - max_pairs} more")
    lines.append(f"Duplicates merged (thin + detail page): {len(dups)}")
    lines += [f"  {d['detail_id']} <- {d['thin_id']} ({d['score']:.3f})" for d in dups[:max_pairs]]
    if len(dups) > max_pairs:
        lines.append(f"  ... {len(dups) - max_pairs} more")
    lines.append(f"Full report: {path}")
    return "\n".join(lines)


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
            out = pipeline.run(data, include_outputs_from={"discoverer", "judge", "detector", "detail_fetcher",
                                                          "extractor", "writer"})
            chunks += 1
            det = out["detector"]
            if dry_run:
                run.extra["dry_run"] = {"detail_candidates": len(det["detail"]), "thin_to_write": len(det["thin"]),
                                        "delisted": len(det["delisted"])}
                report = out.get("judge", {}).get("report") or out.get("discoverer", {}).get("report")
                if report is not None:
                    run.extra["dry_run"]["discovery"] = save_discovery_report(settings.logs_dir, run.run_id, report)
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
        # Dossier Qdrant verrouille par un autre processus : echouer maintenant, pas apres l'heure de vectorisation.
        store = getattr(pipeline.get_component("vector_writer"), "document_store", None)
        if hasattr(store, "check_lock"):
            store.check_lock()
        out = pipeline.run({"loader": {"full": full}},
                           include_outputs_from={"loader", "chunker", "vector_writer"})
        run.extra.update({"mode": "index", "full": out["loader"]["full"],
                          "stats": out["vector_writer"]["stats"], "skipped": out["chunker"]["skipped"]})
    return run.final_summary


def run_deliverables(settings: Settings, *, full: bool = False, limit: int | None = None,
                     chunk_size: int | None = None, model: str | None = None, run_id: str | None = None,
                     pipeline_factory: PipelineFactory = build_deliverables_pipeline) -> dict[str, Any]:
    """Livrables des projets, par lots : chaque lot est ecrit avant le suivant (reprise sur limite de souscription)."""
    chunk = chunk_size if chunk_size is not None else settings.chunk_size
    with runlog.open_run(settings.logs_dir, run_id) as run:
        pipeline = pipeline_factory(settings, model=model)
        budget = limit
        chunks = 0
        quota = False
        remaining = 0
        written: dict[str, int] = {}
        failed: list[str] = []
        while True:
            n = chunk if budget is None else min(chunk, budget)
            out = pipeline.run({"publications": {"full": full, "limit": n}},
                               include_outputs_from={"publications", "texts", "summarizer", "writer"})
            chunks += 1
            sent = len(out["publications"]["to_process"])
            remaining = out["publications"]["remaining"]
            for key, value in out["writer"]["stats"].items():
                written[key] = written.get(key, 0) + value
            failed += out["publications"]["failed"] + out["texts"]["failed"] + out["summarizer"]["failed"]
            quota = bool(out["summarizer"]["quota_exceeded"])
            if budget is not None:
                budget -= sent
            if quota or remaining == 0 or sent == 0 or (budget is not None and budget <= 0):
                break
        run.extra.update({"mode": "deliverables", "chunks": chunks, "quota_exceeded": quota, "written": written,
                          "remaining_publications": remaining, "failed": sorted(set(failed))})
    return run.final_summary


def refresh_deliverables_metadata(settings: Settings, run_id: str | None = None) -> dict[str, Any]:
    """Met a jour pays, annee, programme et domaine dans le front matter des textes integraux (ni LLM ni reseau)."""
    with runlog.open_run(settings.logs_dir, run_id) as run:
        root = Path(settings.output_dir)
        projects = shared_store(settings.output_dir)
        files = updated = 0
        for entry in DeliverableStore(settings.output_dir).all().values():
            path = root / entry.full_text_path
            if not path.exists():
                runlog.event("refresh_deliverables_metadata", "alert", entry.publication_id, "warning",
                             message=f"full text file missing: {entry.full_text_path}")
                continue
            files += 1
            text = path.read_text(encoding="utf-8")
            fm, body = parse_front_matter(text.replace("\r\n", "\n"))
            meta = project_meta(entry.project_ids, projects)
            if all(fm.get(k) == meta[k] for k in META_FIELDS) and all(k in fm for k in META_FIELDS):
                continue
            fm.update({k: meta[k] for k in META_FIELDS})
            _retry_on_lock(lambda: path.write_text(render(fm, body), encoding="utf-8"))
            updated += 1
            runlog.event("refresh_deliverables_metadata", "write", entry.publication_id, "updated")
        run.extra.update({"mode": "refresh_metadata", "files": files, "updated": updated})
    return {"files": files, "updated": updated}
