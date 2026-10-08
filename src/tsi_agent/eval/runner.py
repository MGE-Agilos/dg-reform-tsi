"""Commande `eval-retrieval` : questions, variantes de recherche, taille des morceaux, rapport."""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from tsi_agent import runlog
from tsi_agent.config import Settings
from tsi_agent.eval import report
from tsi_agent.eval.bench import CANDIDATES, TOP_K, build_bm25_store, run_variants
from tsi_agent.eval.chunk_sizes import WINDOWS, evaluate_chunk_sizes
from tsi_agent.eval.questions import (QUESTION_MODEL, default_question_generator, generate_questions,
                                      load_questions, save_questions)
from tsi_agent.eval.sampling import load_all_documents, sample_chunks
from tsi_agent.qdrant_local import collection_exists, open_store

LOCKED = ("base Qdrant deja ouverte par un autre processus : arretez le serveur du chatbot "
          "(python -m tsi_agent chat) ou l'index en cours, puis relancez la mesure")


def open_eval_store(settings: Settings, store: Any = None) -> Any:
    """Store de la couche vectorisee, verrou verifie avant tout travail (un seul processus sur la base)."""
    if store is None:
        if not collection_exists(settings.vectors_dir):
            raise RuntimeError(f"couche vectorisee absente ({settings.vectors_dir}) : "
                               "lancez d'abord python -m tsi_agent index")
        store = open_store(settings.vectors_dir, settings.embedding_dim)
    if hasattr(store, "check_lock"):
        try:
            store.check_lock()
        except RuntimeError as exc:
            raise RuntimeError(LOCKED) from exc
    return store


def run_eval(settings: Settings, *, out_dir: str | Path = "output/eval", questions_file: str | Path | None = None,
             regenerate: bool = False, skip_chunk_sizes: bool = False, n_project: int = 60, n_deliverable: int = 60,
             n_other: int = 30, min_words: int = 60, store: Any = None, text_embedder: Any = None,
             document_embedder: Any = None, generator: Any = None, projects_root: str | Path | None = None,
             windows: tuple[int, ...] = WINDOWS, today: str | None = None, run_id: str | None = None,
             progress: Callable[[str], None] | None = print) -> dict[str, Any]:
    say = progress or (lambda _msg: None)
    store = open_eval_store(settings, store)
    out_dir = Path(out_dir)
    qfile = Path(questions_file) if questions_file else out_dir / "questions.jsonl"
    today = today or date.today().isoformat()
    with runlog.open_run(settings.logs_dir, run_id) as run:
        say("Lecture de tous les morceaux de la base...")
        documents = load_all_documents(store)
        say(f"{len(documents)} morceaux")
        if qfile.exists() and not regenerate:
            questions = load_questions(qfile)
            say(f"Questions reutilisees : {qfile} ({len(questions)})")
        else:
            chunks = sample_chunks(documents, n_project, n_deliverable, n_other, min_words=min_words)
            say(f"Redaction de {len(chunks)} questions ({QUESTION_MODEL})...")
            questions = generate_questions(chunks, generator or default_question_generator(settings))
            save_questions(questions, qfile)
            say(f"{len(questions)} questions enregistrees : {qfile}")
        if not questions:
            raise RuntimeError("aucune question : jeu de questions vide")
        if text_embedder is None:
            from tsi_agent.chat.pipeline import default_text_embedder
            text_embedder = default_text_embedder(settings)
        say("Index BM25 en memoire...")
        bm25 = build_bm25_store(documents)
        del documents
        rows = run_variants(questions, store, text_embedder, bm25_store=bm25, top_k=TOP_K, candidates=CANDIDATES,
                            progress=say)
        sizes = None
        if not skip_chunk_sizes:
            if document_embedder is None:
                from tsi_agent.index_pipeline import default_embedder
                document_embedder = default_embedder(settings)
            sizes = evaluate_chunk_sizes(questions, projects_root or settings.output_dir, document_embedder,
                                         text_embedder, windows=windows, progress=say)
        setup = {"date": today, "index_size": store.count_documents(), "question_model": QUESTION_MODEL,
                 "embedding_model": settings.embedding_model, "top_k": TOP_K, "candidates": CANDIDATES,
                 "questions_file": str(qfile)}
        md_path, csv_path = out_dir / f"retrieval-{today}.md", out_dir / f"retrieval-{today}.csv"
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(report.render_report(rows, setup, sizes), encoding="utf-8")
        report.write_csv(rows, csv_path)
        run.extra.update({"mode": "eval_retrieval", "questions": len(questions), "report": str(md_path)})
    summary = run.final_summary or {}
    return {"report": str(md_path), "csv": str(csv_path), "questions_file": str(qfile), "questions": len(questions),
            "cost_usd": summary.get("cost_usd", 0.0), "run_id": run.run_id}
