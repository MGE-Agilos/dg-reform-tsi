# src/tsi_agent/vector_state.py
"""Etat de la couche vectorisee : `<vectors_dir>/state.json` (fichiers indexes) et `store.json` (morceaux)."""

import json
import os
from pathlib import Path

from haystack import Document
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack.document_stores.types import DuplicatePolicy
from pydantic import BaseModel, Field

from tsi_agent.store import _retry_on_lock

STATE_FILE = "state.json"
STORE_FILE = "store.json"


class VectorLayerError(RuntimeError):
    pass


class FileState(BaseModel):
    hash: str
    chunk_ids: list[str] = Field(default_factory=list)


class VectorState(BaseModel):
    embedder_id: str | None = None
    chunker_version: int | None = None
    files: dict[str, FileState] = Field(default_factory=dict)


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    def write_and_replace() -> None:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    _retry_on_lock(write_and_replace)


def store_path(vectors_dir: str | Path) -> Path:
    return Path(vectors_dir) / STORE_FILE


def load_state(vectors_dir: str | Path) -> VectorState | None:
    path = Path(vectors_dir) / STATE_FILE
    if not path.exists():
        return None
    try:
        return VectorState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError : couvre aussi pydantic.ValidationError
        raise VectorLayerError(f"couche vectorisee illisible ({path}) : relancer avec --full") from exc


def save_state(vectors_dir: str | Path, state: VectorState) -> None:
    data = state.model_dump()
    data["files"] = dict(sorted(data["files"].items()))
    _write_atomic(Path(vectors_dir) / STATE_FILE, json.dumps(data, indent=1, ensure_ascii=False))


def new_store() -> InMemoryDocumentStore:
    # shared=False : pas de stockage partage entre instances du processus (tests, runs successifs).
    return InMemoryDocumentStore(embedding_similarity_function="cosine", shared=False)


def read_documents(path: Path) -> list[Document]:
    """Morceaux de `store.json` (format InMemoryDocumentStore.save_to_disk)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [Document.from_dict(d) for d in data["documents"]]
    except FileNotFoundError as exc:
        raise VectorLayerError(f"couche vectorisee absente ({path}) : lancez d'abord python -m tsi_agent index") \
            from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VectorLayerError(f"couche vectorisee illisible ({path}) : relancer avec --full") from exc


def load_store(path: Path) -> InMemoryDocumentStore:
    store = new_store()
    store.write_documents(read_documents(path), policy=DuplicatePolicy.OVERWRITE)
    return store


def save_store(store: InMemoryDocumentStore, path: Path) -> None:
    """Meme format que InMemoryDocumentStore.save_to_disk, relisible par load_from_disk (futur chatbot)."""
    data = store.to_dict()
    data["documents"] = [d.to_dict(flatten=False) for d in store.filter_documents()]
    _write_atomic(path, json.dumps(data, ensure_ascii=False))
