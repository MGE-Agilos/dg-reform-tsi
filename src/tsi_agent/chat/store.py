"""Document store local du chatbot : InMemoryDocumentStore charge depuis output/vectors/store.json.

Serialise sous la forme de son seul chemin (le YAML reste petit). Dans deepset, remplace par le document store
de l'espace de travail.
"""

from pathlib import Path
from typing import Any

from haystack import default_from_dict, default_to_dict
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack.document_stores.types import DuplicatePolicy

from tsi_agent.vector_state import read_documents


class PersistedInMemoryDocumentStore(InMemoryDocumentStore):
    def __init__(self, path: str = "output/vectors/store.json"):
        self.path = path
        # Stockage partage par chemin : les outils (et un rechargement YAML) ne chargent le fichier qu'une fois.
        super().__init__(embedding_similarity_function="cosine", index=f"tsi:{Path(path).resolve()}", shared=True)
        if self.count_documents() == 0:
            self.write_documents(read_documents(Path(path)), policy=DuplicatePolicy.OVERWRITE)

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, path=self.path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersistedInMemoryDocumentStore":
        return default_from_dict(cls, data)
