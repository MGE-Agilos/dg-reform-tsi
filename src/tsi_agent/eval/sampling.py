"""Tirage stratifie et reproductible des morceaux qui serviront de reponses attendues."""

import random
from typing import Any

from haystack import Document

STRATA = ("project", "deliverable", "other")


def stratum(doc: Document) -> str | None:
    """project (texte de projet), deliverable (texte integral), other (programme ou resume IA)."""
    doc_type = doc.meta.get("doc_type")
    if doc_type == "programme" or doc.meta.get("ai_generated"):
        return "other"
    if doc_type in ("project", "deliverable"):
        return doc_type
    return None


def sample_chunks(documents: list[Document], n_project: int = 60, n_deliverable: int = 60, n_other: int = 30,
                  min_words: int = 60, seed: int = 42) -> list[Document]:
    """Tirage par strate (projets, livrables, autres) ; une strate trop petite est prise en entier."""
    pools: dict[str, list[Document]] = {s: [] for s in STRATA}
    # Tri par id : le tirage ne depend pas de l'ordre de lecture du store.
    for doc in sorted(documents, key=lambda d: d.id):
        s = stratum(doc)
        if s and len((doc.content or "").split()) >= min_words:
            pools[s].append(doc)
    rng = random.Random(seed)
    sample: list[Document] = []
    for s, n in zip(STRATA, (n_project, n_deliverable, n_other)):
        sample += rng.sample(pools[s], min(n, len(pools[s])))
    return sample


def load_all_documents(store: Any) -> list[Document]:
    """Tous les morceaux du store, sans leurs vecteurs (inutiles ici et lourds : ~80 000 x 384 flottants)."""
    if hasattr(store, "_initialize_client") and hasattr(store, "index"):
        from haystack_integrations.document_stores.qdrant.converters import \
            convert_qdrant_point_to_haystack_document
        store._initialize_client()
        docs: list[Document] = []
        offset = None
        while True:
            records, offset = store._client.scroll(collection_name=store.index, limit=1000, offset=offset,
                                                   with_payload=True, with_vectors=False)
            docs += [convert_qdrant_point_to_haystack_document(r, use_sparse_embeddings=False) for r in records]
            if offset is None:
                return docs
    return [Document(id=d.id, content=d.content, meta=d.meta) for d in store.filter_documents()]
