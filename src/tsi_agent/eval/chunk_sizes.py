"""Taille des morceaux : redecoupage des projets + programme, index InMemory temporaire, questions de projets.

Le morceau attendu n'existe pas dans les autres decoupages : on mesure au niveau fichier (doc_id) et au niveau
passage (le morceau trouve, du bon fichier, contient au moins la moitie des mots de la phrase-cle du morceau
d'origine).
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from haystack import Document
from haystack.components.retrievers.in_memory import InMemoryEmbeddingRetriever
from haystack.document_stores.in_memory import InMemoryDocumentStore

from tsi_agent.chunking import chunk_file
from tsi_agent.eval import metrics

WINDOWS = (150, 250, 400)
FOLDERS = ("projects", "programme")
KS = (8, 24)
EMBED_BATCH = 256


def split_params(window: int) -> dict[str, int]:
    """Chevauchement proportionnel (40 mots pour 250) ; seuil de decoupage = fenetre + 50 (300 pour 250)."""
    return {"threshold": window + 50, "window": window, "overlap": round(window * 0.16)}


def chunk_corpus(root: str | Path, window: int) -> list[Document]:
    """Morceaux des fichiers projets et programme de `root`, avec la fenetre donnee ; fichiers illisibles ignores."""
    root = Path(root)
    params = split_params(window)
    docs: list[Document] = []
    for folder in FOLDERS:
        for path in sorted((root / folder).glob("*.md")):
            try:
                docs += chunk_file(f"{folder}/{path.name}", path.read_text(encoding="utf-8"), **params)
            except (ValueError, UnicodeDecodeError, yaml.YAMLError):
                continue
    return docs


def _passage_hit(docs: list[Document], question: dict[str, Any]) -> int | None:
    for rank, doc in enumerate(docs, start=1):
        if doc.meta.get("doc_id") == question["doc_id"] and \
                metrics.text_overlap_hit(doc.content or "", question.get("key_text") or ""):
            return rank
    return None


def evaluate_chunk_sizes(questions: list[dict[str, Any]], root: str | Path, document_embedder: Any,
                         text_embedder: Any, windows: tuple[int, ...] = WINDOWS, batch_size: int = EMBED_BATCH,
                         progress: Callable[[str], None] | None = print) -> list[dict[str, Any]]:
    """Recall fichier et passage a k=8 et 24 pour chaque taille de fenetre (questions de projets seulement)."""
    say = progress or (lambda _msg: None)
    selected = [q for q in questions if q.get("stratum") == "project"]
    for component in (document_embedder, text_embedder):
        if hasattr(component, "warm_up"):
            component.warm_up()
    embeddings = [text_embedder.run(text=q["question"])["embedding"] for q in selected]
    results: list[dict[str, Any]] = []
    for window in windows:
        docs = chunk_corpus(root, window)
        say(f"Taille {window} mots : {len(docs)} morceaux a vectoriser")
        store = InMemoryDocumentStore(embedding_similarity_function="cosine")
        t0 = time.monotonic()
        for start in range(0, len(docs), batch_size):
            store.write_documents(document_embedder.run(documents=docs[start:start + batch_size])["documents"])
            done = min(start + batch_size, len(docs))
            elapsed = time.monotonic() - t0
            eta = elapsed / done * (len(docs) - done)
            say(f"  taille {window} : {done}/{len(docs)} morceaux ({elapsed:.0f} s, reste ~{eta:.0f} s)")
        retriever = InMemoryEmbeddingRetriever(document_store=store)
        file_ranks: list[int | None] = []
        passage_ranks: list[int | None] = []
        for q, embedding in zip(selected, embeddings):
            found = retriever.run(query_embedding=embedding, top_k=max(KS))["documents"]
            file_ranks.append(metrics.file_rank(found, q["doc_id"]))
            passage_ranks.append(_passage_hit(found, q))
        row: dict[str, Any] = {**split_params(window), "chunks": len(docs), "questions": len(selected)}
        for k in KS:
            row[f"file@{k}"] = metrics.recall_at(file_ranks, k)
            row[f"passage@{k}"] = metrics.recall_at(passage_ranks, k)
        results.append(row)
    return results
