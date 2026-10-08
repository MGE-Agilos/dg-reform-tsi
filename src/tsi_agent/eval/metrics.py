"""Rangs, recall@k, MRR, fusion RRF, diversification et recouvrement de texte."""

import re
from collections.abc import Iterable, Sequence
from typing import Any

from haystack import Document

KS = (1, 3, 5, 8, 16, 24)
_WORD = re.compile(r"\w+")


def chunk_key(doc: Document) -> str:
    """Identite d'un morceau : `<chemin sans .md>#n`, comme l'id pose par le decoupage.

    qdrant-haystack garde l'id d'origine dans le payload (Document.id est donc conserve) ; on reconstruit tout de
    meme la cle depuis source_file + chunk_index quand ils existent, pour ne pas dependre de ce detail.
    """
    source, index = doc.meta.get("source_file"), doc.meta.get("chunk_index")
    if source and index is not None:
        return f"{str(source).removesuffix('.md')}#{index}"
    return doc.id


def _first(docs: Sequence[Document], hit: Any) -> int | None:
    return next((i for i, doc in enumerate(docs, start=1) if hit(doc)), None)


def chunk_rank(docs: Sequence[Document], chunk_id: str) -> int | None:
    """Rang (a partir de 1) du morceau attendu ; None s'il est absent."""
    return _first(docs, lambda doc: chunk_key(doc) == chunk_id)


def file_rank(docs: Sequence[Document], doc_id: str) -> int | None:
    return _first(docs, lambda doc: doc.meta.get("doc_id") == doc_id)


def project_rank(docs: Sequence[Document], projects: Iterable[str]) -> int | None:
    """Premier resultat lie a l'un des projets attendus (le projet lui-meme ou l'un de ses livrables)."""
    wanted = set(projects or [])
    if not wanted:
        return None
    return _first(docs, lambda doc: doc.meta.get("doc_id") in wanted or bool(wanted & set(doc.meta.get("projects") or [])))


def recall_at(ranks: Sequence[int | None], k: int) -> float | None:
    if not ranks:
        return None
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def mrr(ranks: Sequence[int | None]) -> float | None:
    if not ranks:
        return None
    return sum(1 / r for r in ranks if r) / len(ranks)


def summarize(ranks: Sequence[int | None], ks: Sequence[int] = KS) -> dict[str, Any]:
    return {"n": len(ranks), **{f"r@{k}": recall_at(ranks, k) for k in ks}, "mrr": mrr(ranks)}


def group_by(rows: Iterable[dict[str, Any]], field: str) -> dict[Any, list[dict[str, Any]]]:
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get(field), []).append(row)
    return groups


def rrf_fuse(lists: Sequence[Sequence[Document]], k: int = 60) -> list[Document]:
    """Reciprocal Rank Fusion : somme des 1/(k + rang) ; a egalite, ordre de premiere apparition."""
    scores: dict[str, float] = {}
    docs: dict[str, Document] = {}
    for ranked in lists:
        for rank, doc in enumerate(ranked, start=1):
            key = chunk_key(doc)
            docs.setdefault(key, doc)
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank)
    return [docs[key] for key in sorted(scores, key=lambda key: -scores[key])]


def diversify(docs: Sequence[Document], max_per_file: int = 2, top_k: int = 24) -> list[Document]:
    """Au plus `max_per_file` morceaux par fichier (doc_id), dans l'ordre, jusqu'a `top_k` resultats."""
    seen: dict[Any, int] = {}
    out: list[Document] = []
    for doc in docs:
        file = doc.meta.get("doc_id")
        if seen.get(file, 0) >= max_per_file:
            continue
        seen[file] = seen.get(file, 0) + 1
        out.append(doc)
        if len(out) >= top_k:
            break
    return out


def _words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def overlap_share(found_content: str, key_text: str) -> float:
    """Part des mots (distincts) de la phrase-cle presents dans le texte trouve ; casse et ponctuation ignorees."""
    key = _words(key_text)
    if not key:
        return 0.0
    return len(key & _words(found_content)) / len(key)


def text_overlap_hit(found_content: str, key_text: str, threshold: float = 0.5) -> bool:
    return bool(_words(key_text)) and overlap_share(found_content, key_text) >= threshold
