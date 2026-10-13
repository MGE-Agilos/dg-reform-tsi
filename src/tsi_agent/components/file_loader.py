# src/tsi_agent/components/file_loader.py
"""Composant Haystack local : fichiers extraits nouveaux, modifies ou supprimes depuis le dernier index.

Dans deepset, ce role est tenu par l'espace de travail (les fichiers ajoutes ou modifies sont indexes).
"""

import hashlib
from pathlib import Path
from typing import Any

from haystack import component
from haystack.dataclasses import ByteStream

from tsi_agent import runlog
from tsi_agent.chunking import CHUNKER_VERSION
from tsi_agent.qdrant_local import collection_exists
from tsi_agent.vector_state import VectorState, load_state

FOLDERS = ("projects", "programme", "deliverables", "csr")


@component
class ProjectFileLoader:
    def __init__(self, output_dir: str = "output", vectors_dir: str = "~/tsi-vectors", embedder_id: str = "",
                 chunker_version: int = CHUNKER_VERSION):
        self.output_dir = output_dir
        self.vectors_dir = vectors_dir
        self.embedder_id = embedder_id
        self.chunker_version = chunker_version

    def _full_reason(self, full: bool, state: "VectorState | None") -> str | None:
        if full:
            return "requested"
        if state is None:
            return "no_state"
        if not collection_exists(self.vectors_dir):
            return "no_store"
        if state.embedder_id != self.embedder_id:
            return "embedder_changed"
        if state.chunker_version != self.chunker_version:
            return "chunker_changed"
        return None

    @component.output_types(sources=list[ByteStream], removed=list[str], full=bool)
    def run(self, full: bool = False) -> dict[str, Any]:
        # En --full l'ancien etat ne sert a rien : on ne le lit pas, un state.json corrompu ne doit pas bloquer
        # le run. Sinon on ne le lit qu'une fois (evite de lire deux fois, et de lever deux fois une erreur).
        state = None if full else load_state(Path(self.vectors_dir).expanduser())
        reason = self._full_reason(full, state)
        if reason:
            runlog.event("ProjectFileLoader", "full", None, reason)
        known = {} if reason or state is None else state.files
        root = Path(self.output_dir)
        sources: list[ByteStream] = []
        seen: set[str] = set()
        unchanged = 0
        for folder in FOLDERS:
            for path in sorted((root / folder).glob("*.md")):
                rel = path.relative_to(root).as_posix()
                seen.add(rel)
                data = path.read_bytes()
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                old = known.get(rel)
                if old is not None and old.hash == digest:
                    unchanged += 1
                    continue
                runlog.event("ProjectFileLoader", "load", rel, "changed" if old else "new")
                sources.append(ByteStream(data=data, meta={"file_path": rel, "content_hash": digest}))
        removed = sorted(set(known) - seen)
        for rel in removed:
            runlog.event("ProjectFileLoader", "load", rel, "removed")
        runlog.event("ProjectFileLoader", "load", None, "unchanged", files=unchanged)
        return {"sources": sources, "removed": removed, "full": reason is not None}
