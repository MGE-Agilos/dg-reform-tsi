# src/tsi_agent/components/vector_writer.py
"""Composant Haystack local : ecrit les morceaux vectorises dans le document store (Qdrant local) puis state.json.

Dans deepset, remplace par un DocumentWriter vers le document store de l'espace de travail.
"""

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict, import_class_by_name
from haystack.dataclasses import ByteStream
from haystack.document_stores.types import DuplicatePolicy

from tsi_agent import runlog
from tsi_agent.chunking import CHUNKER_VERSION
from tsi_agent.vector_state import FileState, VectorState, load_state, save_state

TOKENS_PER_WORD = 1.33


@component
class VectorLayerWriter:
    def __init__(self, document_store: Any, vectors_dir: str = "~/tsi-vectors", embedder_id: str = "",
                 chunker_version: int = CHUNKER_VERSION, pricing: dict[str, dict[str, float]] | None = None):
        """:param document_store: store des morceaux (Qdrant local ; state.json est ecrit dans vectors_dir).
        :param pricing: $ par million de tokens par prefixe de modele, ex. {"text-embedding-3-small": {"input": 0.02}}."""
        self.document_store = document_store
        self.vectors_dir = vectors_dir
        self.embedder_id = embedder_id
        self.chunker_version = chunker_version
        self.pricing = pricing or {}

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, document_store=component_to_dict(obj=self.document_store, name="document_store"),
                               vectors_dir=self.vectors_dir, embedder_id=self.embedder_id,
                               chunker_version=self.chunker_version, pricing=self.pricing)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VectorLayerWriter":
        store = data["init_parameters"]["document_store"]
        data["init_parameters"]["document_store"] = import_class_by_name(store["type"]).from_dict(store)
        return default_from_dict(cls, data)

    def _cost(self, model: str, tokens: int) -> float:
        for prefix, price in sorted(self.pricing.items(), key=lambda kv: len(kv[0]), reverse=True):
            if model.startswith(prefix):
                return tokens * price.get("input", 0) / 1_000_000
        return 0.0

    def _log_embedding(self, documents: list[Document], meta: dict[str, Any] | None) -> None:
        if not documents:
            return
        meta = meta or {}
        usage = meta.get("usage") or {}
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        estimated = tokens == 0
        if estimated:
            words = sum(len(f"{d.meta.get('context_header') or ''} {d.content or ''}".split()) for d in documents)
            tokens = round(words * TOKENS_PER_WORD)
        model = str(meta.get("model") or self.embedder_id)
        runlog.event("VectorLayerWriter", "embed", model, chunks=len(documents), input_tokens=tokens,
                     tokens_estimated=estimated, model=model, cost_usd=self._cost(model, tokens))

    @component.output_types(stats=dict[str, int])
    def run(self, documents: list[Document], sources: list[ByteStream], removed: list[str], full: bool = False,
            skipped: list[str] | None = None, embed_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        # Chaque morceau stocke porte l'embedder qui l'a vectorise (spec 3.3) : sert a detecter un changement
        # d'embedder sans dependre uniquement de state.json.
        documents = [replace(d, meta={**d.meta, "embedder": self.embedder_id}) for d in documents]

        vdir = Path(self.vectors_dir).expanduser()
        store = self.document_store
        state = None if full else load_state(vdir)
        if state is None:
            # Full : on repart d'un store vide (ids orphelins d'un run interrompu compris). L'etat est invalide
            # d'abord (embedder vide) : un arret apres le vidage force un full au run suivant, au lieu d'un
            # incremental sur un store a moitie vide.
            state = VectorState()
            save_state(vdir, state)
            if hasattr(store, "reset_collection"):
                store.reset_collection()
            else:
                store.delete_all_documents()
        state.embedder_id, state.chunker_version = self.embedder_id, self.chunker_version

        touched = [str(s.meta["file_path"]) for s in sources] + list(removed)
        stale = [cid for rel in touched if rel in state.files for cid in state.files[rel].chunk_ids]
        if stale:
            store.delete_documents(stale)
        if documents:
            store.write_documents(documents, policy=DuplicatePolicy.OVERWRITE)

        by_file: dict[str, list[str]] = defaultdict(list)
        for doc in documents:
            by_file[doc.meta["source_file"]].append(doc.id)
        skipped_set = set(skipped or [])
        for src in sources:
            rel = str(src.meta["file_path"])
            if rel in skipped_set:
                state.files.pop(rel, None)  # retente au prochain run
            else:
                state.files[rel] = FileState(hash=src.meta["content_hash"], chunk_ids=by_file.get(rel, []))
        for rel in removed:
            state.files.pop(rel, None)

        self._log_embedding(documents, embed_meta)
        if full or sources or removed:
            save_state(vdir, state)  # le store est deja ecrit : un arret avant ceci laisse l'ancien etat, retente
        stats = {"files_indexed": len(sources) - len(skipped_set & {str(s.meta["file_path"]) for s in sources}),
                 "files_removed": len(removed), "chunks_written": len(documents), "chunks_deleted": len(stale),
                 "chunks_total": store.count_documents()}
        runlog.event("VectorLayerWriter", "write", None, **stats)
        return {"stats": stats}
