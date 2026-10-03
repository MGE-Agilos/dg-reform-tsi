# src/tsi_agent/vector_state.py
"""Etat de la couche vectorisee : `<vectors_dir>/state.json` (fichiers indexes) ; les morceaux sont dans Qdrant (`<vectors_dir>/qdrant`)."""

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from tsi_agent.store import _retry_on_lock

STATE_FILE = "state.json"


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
