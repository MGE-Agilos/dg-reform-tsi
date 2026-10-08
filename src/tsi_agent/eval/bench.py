"""Variantes de recherche mesurees sur l'index actuel (sans revectorisation).

A : semantique seule (comme le chatbot) ; B : semantique + filtre pays ; C : hybride semantique + BM25 fusionnes
par RRF ; D : semantique, au plus 2 morceaux par fichier.
"""

import time
from collections.abc import Callable
from typing import Any

from haystack import Document
from haystack.document_stores.in_memory import InMemoryDocumentStore

from tsi_agent.chat.tools import build_filters
from tsi_agent.eval import metrics
from tsi_agent.eval.sampling import load_all_documents

VARIANTS = ("A", "B", "C", "D")
CANDIDATES = 50
MAX_PER_FILE = 2
TOP_K = 24


def build_bm25_store(documents: list[Document]) -> InMemoryDocumentStore:
    """Index BM25 en memoire sur le texte de tous les morceaux (en-tete de contexte + contenu)."""
    store = InMemoryDocumentStore()
    store.write_documents([
        Document(id=metrics.chunk_key(d),
                 content="\n".join(p for p in (d.meta.get("context_header"), d.content) if p),
                 meta={k: d.meta.get(k) for k in ("doc_id", "doc_type", "projects", "title", "section", "page",
                                                  "source_file", "chunk_index", "countries")})
        for d in documents])
    return store


def expected_projects(question: dict[str, Any]) -> list[str]:
    """Projets attendus : le projet lui-meme, ou les projets d'un livrable ; aucun pour une page programme."""
    if question.get("doc_type") == "project":
        return [question["doc_id"]]
    return list(question.get("projects") or [])


def _top3(docs: list[Document]) -> list[dict[str, Any]]:
    return [{"doc_id": d.meta.get("doc_id"), "title": d.meta.get("title"), "section": d.meta.get("section"),
             "page": d.meta.get("page")} for d in docs[:3]]


def run_variants(questions: list[dict[str, Any]], store: Any, text_embedder: Any,
                 bm25_store: InMemoryDocumentStore | None = None, top_k: int = TOP_K,
                 candidates: int = CANDIDATES, retriever: Any = None,
                 progress: Callable[[str], None] | None = print) -> list[dict[str, Any]]:
    """Rangs du bon morceau, du bon fichier et du bon projet pour chaque question et chaque variante."""
    say = progress or (lambda _msg: None)
    if retriever is None:
        from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever
        retriever = LocalQdrantEmbeddingRetriever(document_store=store)
    if bm25_store is None:
        say("Index BM25 : lecture de tous les morceaux...")
        bm25_store = build_bm25_store(load_all_documents(store))
    if hasattr(text_embedder, "warm_up"):
        text_embedder.warm_up()
    t0 = time.monotonic()
    rows: list[dict[str, Any]] = []
    for n, q in enumerate(questions, start=1):
        embedding = text_embedder.run(text=q["question"])["embedding"]
        semantic = retriever.run(query_embedding=embedding, top_k=candidates)["documents"]
        countries = q.get("countries") or []
        if countries:
            filtered = retriever.run(query_embedding=embedding, top_k=top_k,
                                     filters=build_filters(doc_type=None, countries=countries))["documents"]
        else:
            filtered = semantic[:top_k]
        lexical = bm25_store.bm25_retrieval(query=q["question"], top_k=candidates)
        results = {"A": semantic[:top_k], "B": filtered,
                   "C": metrics.rrf_fuse([semantic, lexical])[:top_k],
                   "D": metrics.diversify(semantic, MAX_PER_FILE, top_k)}
        row = {k: q.get(k) for k in ("id", "lang", "stratum", "doc_type", "ai_generated", "chunk_id", "doc_id",
                                     "title", "section", "page", "question")}
        projects = expected_projects(q)
        for v, docs in results.items():
            row[f"{v}_chunk"] = metrics.chunk_rank(docs, q["chunk_id"])
            row[f"{v}_file"] = metrics.file_rank(docs, q["doc_id"])
            row[f"{v}_project"] = metrics.project_rank(docs, projects)
        row["has_project"] = bool(projects)
        row["B_filtered"] = bool(countries)
        row["A_top3"] = _top3(results["A"])
        rows.append(row)
        if n % 10 == 0 or n == len(questions):
            say(f"  recherche : {n}/{len(questions)} questions ({time.monotonic() - t0:.0f} s)")
    return rows
