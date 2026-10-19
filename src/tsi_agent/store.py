"""État persistant : quels projets sont connus, avec quels hash, et où sont leurs fichiers."""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


MAX_FILE_STEM = 100  # limite de chemin Windows (260) : le dossier du dépôt en consomme déjà ~110
REPLACE_ATTEMPTS = 8  # OneDrive et antivirus peuvent verrouiller le fichier temporairement


def _retry_on_lock(fn):
    """Réessaie `fn()` avec backoff sur PermissionError transitoire (OneDrive/antivirus)."""
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            return fn()
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                raise
            time.sleep(0.5 * attempt)


def file_stem(project_id: str) -> str:
    """Nom de fichier sûr : l'id complet reste dans le front matter."""
    if len(project_id) <= MAX_FILE_STEM:
        return project_id
    digest = hashlib.sha1(project_id.encode("utf-8")).hexdigest()[:8]
    return f"{project_id[:MAX_FILE_STEM - 9].rstrip('-')}-{digest}"


class StoreEntry(BaseModel):
    project_id: str
    file: str
    programme: str | None = None
    detail_page: bool = False
    content_hash: str | None = None
    listing_hash: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    first_seen: str
    last_updated: str
    listed: bool = True
    aliases: list[str] = Field(default_factory=list)
    # Date <lastmod> du sitemap de la page detail lors de sa derniere lecture (mode incremental de `run`).
    sitemap_lastmod: str | None = None
    # Titre et annee officiels (page detail) : indices de la fusion floue quand la page n'est pas relue.
    title: str | None = None
    year: int | None = None
    # Liens de publication lus sur la page detail (etage livrables) : repris quand la page n'est pas relue.
    publication_urls: list[str] | None = None


class ProjectStore(Protocol):
    def get(self, project_id: str) -> StoreEntry | None: ...
    def all(self) -> dict[str, StoreEntry]: ...
    def put(self, entry: StoreEntry, save: bool = True) -> None: ...
    def flush(self) -> None: ...
    def remove(self, project_id: str) -> None: ...
    def write_project_file(self, project_id: str, text: str) -> str: ...
    def read_project_file(self, project_id: str) -> str | None: ...
    def delete_project_file(self, project_id: str) -> None: ...
    def programme_hash(self, doc_id: str) -> str | None: ...
    def write_programme_doc(self, doc_id: str, text: str, content_hash: str) -> str: ...


class LocalProjectStore:
    """Fichiers dans `<output_dir>/projects` et `<output_dir>/programme`, index dans `<output_dir>/state/index.json`."""

    def __init__(self, output_dir: str | Path):
        self.root = Path(output_dir)
        self.projects_dir = self.root / "projects"
        self.programme_dir = self.root / "programme"
        self.index_path = self.root / "state" / "index.json"
        for d in (self.projects_dir, self.programme_dir, self.index_path.parent):
            d.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, StoreEntry] = {}
        self._programme: dict[str, str] = {}
        if self.index_path.exists():
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._projects = {k: StoreEntry.model_validate(v) for k, v in data.get("projects", {}).items()}
            self._programme = dict(data.get("programme_docs", {}))

    def get(self, project_id: str) -> StoreEntry | None:
        entry = self._projects.get(project_id)
        return entry.model_copy(deep=True) if entry else None

    def all(self) -> dict[str, StoreEntry]:
        return {k: v.model_copy(deep=True) for k, v in self._projects.items()}

    def put(self, entry: StoreEntry, save: bool = True) -> None:
        self._projects[entry.project_id] = entry.model_copy(deep=True)
        if save:
            self._save()

    def flush(self) -> None:
        self._save()

    def remove(self, project_id: str) -> None:
        if self._projects.pop(project_id, None) is not None:
            self._save()

    def write_project_file(self, project_id: str, text: str) -> str:
        path = self.projects_dir / f"{file_stem(project_id)}.md"
        _retry_on_lock(lambda: path.write_text(text, encoding="utf-8"))
        return path.relative_to(self.root).as_posix()

    def read_project_file(self, project_id: str) -> str | None:
        path = self.projects_dir / f"{file_stem(project_id)}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def delete_project_file(self, project_id: str) -> None:
        path = self.projects_dir / f"{file_stem(project_id)}.md"
        _retry_on_lock(lambda: path.unlink(missing_ok=True))

    def programme_hash(self, doc_id: str) -> str | None:
        return self._programme.get(doc_id)

    def write_programme_doc(self, doc_id: str, text: str, content_hash: str) -> str:
        path = self.programme_dir / f"{doc_id}.md"
        _retry_on_lock(lambda: path.write_text(text, encoding="utf-8"))
        self._programme[doc_id] = content_hash
        self._save()
        return path.relative_to(self.root).as_posix()

    def _save(self) -> None:
        data = {"projects": {k: v.model_dump() for k, v in sorted(self._projects.items())},
                "programme_docs": dict(sorted(self._programme.items()))}
        tmp = self.index_path.with_suffix(".json.tmp")
        payload = json.dumps(data, indent=1, ensure_ascii=False)

        def write_and_replace() -> None:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.index_path)

        _retry_on_lock(write_and_replace)


_shared: dict[str, LocalProjectStore] = {}


def shared_store(output_dir: str | Path) -> LocalProjectStore:
    """Une instance par dossier dans le processus : détecteur et writer voient le même état entre les lots."""
    key = str(Path(output_dir).resolve())
    if key not in _shared:
        _shared[key] = LocalProjectStore(output_dir)
    return _shared[key]
