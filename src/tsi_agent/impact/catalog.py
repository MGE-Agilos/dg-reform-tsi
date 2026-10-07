"""Catalogue des projets pour la page d'analyse d'impact : front matter de output/projects/*.md, recherche simple
par mots (titre, id, pays, annee, programme)."""

import unicodedata
from pathlib import Path
from typing import Any

import yaml

from tsi_agent.countries import COUNTRIES, name_from_iso

FIELDS = ("project_id", "title", "year", "programme", "countries", "policy_area", "detail_page")


def _front_matter(path: Path) -> dict[str, Any] | None:
    """Front matter seul (lecture ligne a ligne jusqu'au second ---) : les fichiers projet peuvent etre longs."""
    lines: list[str] = []
    with path.open(encoding="utf-8") as fh:
        if fh.readline().rstrip("\n") != "---":
            return None
        for line in fh:
            if line.rstrip("\n") == "---":
                return yaml.safe_load("".join(lines)) or {}
            lines.append(line)
    return None


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def load_catalog(projects_dir: str | Path) -> list[dict[str, Any]]:
    catalog = []
    for path in sorted(Path(projects_dir).glob("*.md")):
        try:
            fm = _front_matter(path)
        except (OSError, yaml.YAMLError):
            continue
        if not fm or not fm.get("project_id"):
            continue
        entry = {k: fm.get(k) for k in FIELDS}
        names = [name_from_iso(c) for c in entry.get("countries") or [] if c in COUNTRIES]
        entry["_search"] = _fold(" ".join(str(v) for v in (entry["title"], entry["project_id"], entry["year"],
                                                            entry["programme"], *(entry.get("countries") or []),
                                                            *names) if v))
        catalog.append(entry)
    return catalog


def search_catalog(catalog: list[dict[str, Any]], query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Projets dont le texte de recherche contient tous les mots ; pages detaillees puis plus recents d'abord."""
    words = _fold(query or "").split()
    hits = [p for p in catalog if all(w in p["_search"] for w in words)]
    hits.sort(key=lambda p: (not p.get("detail_page"), -(p.get("year") or 0), str(p.get("title"))))
    return [{k: p[k] for k in FIELDS} for p in hits[:limit]]
