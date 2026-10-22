"""Catalogue des projets pour la page d'analyse d'impact : front matter de output/projects/*.md, recherche simple
par mots (titre, id, pays, annee, programme)."""

import json
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from tsi_agent.chunking import INTRO, SUMMARY, split_sections
from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.render import parse_front_matter

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


SUMMARY_CHARS = 1200


def load_project_records(output_dir: str | Path) -> list[dict[str, Any]]:
    """Projets du store local pour les traitements en lot (impact-batch, csr-related) : champs du catalogue, plus
    `results`, `csrs` (liens CSR ecrits par csr-link) et `summary` (resume IA, a defaut l'introduction).

    Seuls les projets connus de l'etat (output/state/index.json) sont gardes quand il existe."""
    root = Path(output_dir)
    known: set[str] | None = None
    index = root / "state" / "index.json"
    if index.exists():
        try:
            known = set((json.loads(index.read_text(encoding="utf-8")).get("projects") or {}))
        except (OSError, ValueError):
            known = None
    records = []
    for path in sorted((root / "projects").glob("*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(fm, dict) or not fm.get("project_id"):
            continue
        if known is not None and fm["project_id"] not in known:
            continue
        sections = dict(split_sections(body))
        summary = sections.get(SUMMARY) or sections.get(INTRO) or ""
        csrs = fm.get("csrs")
        records.append({**{k: fm.get(k) for k in FIELDS}, "countries": list(fm.get("countries") or []),
                        "year": fm.get("year") if isinstance(fm.get("year"), int) else None,
                        "results": fm.get("results"), "csrs": list(csrs) if isinstance(csrs, list) else [],
                        "summary": " ".join(summary.split())[:SUMMARY_CHARS]})
    return records
