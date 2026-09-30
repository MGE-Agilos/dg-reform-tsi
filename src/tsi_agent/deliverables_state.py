"""Etat de l'etage livrables : output/state/deliverables.json (une entree par publication traitee)."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tsi_agent.models import DeliverableContent
from tsi_agent.store import _retry_on_lock


class DeliverableEntry(BaseModel):
    publication_id: str
    url: str
    title: str
    date: str | None = None
    project_ids: list[str] = Field(default_factory=list)
    files_key: str
    files: list[dict[str, Any]] = Field(default_factory=list)  # document, url, language, filename, pages, words
    full_text_path: str
    content: DeliverableContent
    model: str
    processed: str


def _date_key(entry: DeliverableEntry) -> datetime:
    try:
        return datetime.strptime(entry.date or "", "%d %B %Y")
    except ValueError:
        return datetime.min


class DeliverableStore:
    def __init__(self, output_dir: str | Path):
        self.path = Path(output_dir) / "state" / "deliverables.json"
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self._entries = {k: DeliverableEntry.model_validate(v) for k, v in data.get("publications", {}).items()}

    def get(self, publication_id: str) -> DeliverableEntry | None:
        return self._entries.get(publication_id)

    def put(self, entry: DeliverableEntry) -> None:
        self._entries[entry.publication_id] = entry

    def for_project(self, project_id: str) -> list[DeliverableEntry]:
        """Publications d'un projet, la plus recente d'abord."""
        found = [e for e in self._entries.values() if project_id in e.project_ids]
        return sorted(found, key=lambda e: (_date_key(e), e.publication_id), reverse=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"publications": {k: v.model_dump() for k, v in sorted(self._entries.items())}},
                             indent=1, ensure_ascii=False)
        tmp = self.path.with_suffix(".json.tmp")

        def write_and_replace() -> None:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)

        _retry_on_lock(write_and_replace)
