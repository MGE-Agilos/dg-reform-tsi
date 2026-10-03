"""Qdrant en mode local (dossier, sans serveur) : un seul client par dossier dans le processus.

Qdrant local verrouille son dossier : une 2e instance de QdrantDocumentStore sur le meme `path` echoue, meme dans
le meme processus. Les outils du chatbot et le writer de la couche vectorisee partagent donc un client.
Dans deepset, ces deux classes sont remplacees par le document store et le retriever de l'espace de travail.
"""

import threading
from pathlib import Path
from typing import Any

import qdrant_client
from haystack import component, default_from_dict
from haystack.document_stores.types import FilterPolicy
from haystack.utils import deserialize_secrets_inplace
from haystack_integrations.components.retrievers.qdrant import QdrantEmbeddingRetriever
from haystack_integrations.document_stores.qdrant import QdrantDocumentStore

_CLIENTS: dict[str, qdrant_client.QdrantClient] = {}
_LOCK = threading.Lock()
COLLECTION = "tsi"


def _shared_client(path: str) -> qdrant_client.QdrantClient:
    key = str(Path(path).expanduser().resolve())
    with _LOCK:
        if key not in _CLIENTS:
            try:
                _CLIENTS[key] = qdrant_client.QdrantClient(path=key)
            except RuntimeError as exc:  # dossier verrouille par un autre processus
                raise RuntimeError(f"couche vectorisee {key} deja ouverte par un autre processus "
                                   "(chatbot ou index en cours ?) : arretez-le puis relancez") from exc
        return _CLIENTS[key]


class LocalQdrantDocumentStore(QdrantDocumentStore):
    def check_lock(self) -> None:
        """Ouvre le client partage (sans creer la collection) : echoue tout de suite si le dossier est verrouille."""
        _shared_client(self.path)

    def reset_collection(self) -> None:
        """Vide le store en supprimant puis recreant la collection (bien plus rapide que delete_all_documents).

        Le client local supprime le dossier de la collection avec ignore_errors : sous Windows, la base SQLite encore
        ouverte l'empeche et l'ancien contenu reapparait. On ferme donc la collection avant de la supprimer.
        """
        self._initialize_client()
        local = getattr(self._client, "_client", None)
        collection = getattr(local, "collections", {}).get(self.index)
        if collection is not None and hasattr(collection, "close"):
            collection.close()
        self._client.delete_collection(self.index)
        self._set_up_collection(self.index, self.embedding_dim, self.recreate_index, self.similarity,
                                self.use_sparse_embeddings, self.sparse_idf, self.on_disk,
                                self.payload_fields_to_index)
        if self.count_documents():  # filet : le dossier n'a pas pu etre supprime
            self.delete_all_documents()

    def _initialize_client(self) -> None:
        if self._client is None:
            self._client = _shared_client(self.path)
            self._set_up_collection(self.index, self.embedding_dim, self.recreate_index, self.similarity,
                                    self.use_sparse_embeddings, self.sparse_idf, self.on_disk,
                                    self.payload_fields_to_index)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LocalQdrantDocumentStore":
        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key"])
        return default_from_dict(cls, data)


@component
class LocalQdrantEmbeddingRetriever(QdrantEmbeddingRetriever):
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LocalQdrantEmbeddingRetriever":
        params = data["init_parameters"]
        params["document_store"] = LocalQdrantDocumentStore.from_dict(params["document_store"])
        if policy := params.get("filter_policy"):
            params["filter_policy"] = FilterPolicy.from_str(policy)
        return default_from_dict(cls, data)


def qdrant_path(vectors_dir: str) -> Path:
    return Path(vectors_dir).expanduser() / "qdrant"


def collection_exists(vectors_dir: str) -> bool:
    """La collection existe sur disque (le dossier `qdrant` seul ne prouve rien)."""
    return (qdrant_path(vectors_dir) / "collection" / COLLECTION).is_dir()


def open_store(vectors_dir: str, embedding_dim: int) -> LocalQdrantDocumentStore:
    # Pas de mkdir : construire un store (export YAML, tests) ne doit rien creer ; le client Qdrant cree le dossier.
    return LocalQdrantDocumentStore(path=str(qdrant_path(vectors_dir)), index=COLLECTION, embedding_dim=embedding_dim,
                                    similarity="cosine", progress_bar=False)
