"""Rapport d'anomalies : projets a publier, pages sans livrable, resultats manquants, livrables orphelins, doublons
probables. Deterministe (ni LLM ni reseau) : lit output/projects/*.md et output/state/deliverables.json."""

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date
from itertools import combinations
from pathlib import Path

from tsi_agent.fuzzy import THRESHOLD, title_similarity
from tsi_agent.render import parse_front_matter

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
MD_ROWS_PER_TYPE = 20


@dataclass(frozen=True)
class Anomaly:
    project_id: str
    title: str
    programme: str
    year: int | None
    countries: str
    type: str
    severity: str
    detail: str
    url: str


def _row(p: dict, type_: str, severity: str, detail: str = "") -> Anomaly:
    urls = p.get("source_urls") or []
    return Anomaly(p["project_id"], p.get("title") or "", p.get("programme") or "", p.get("year"),
                   ",".join(p.get("countries") or []), type_, severity, detail, urls[0] if urls else "")


def _age_severity(year: int | None, current_year: int) -> str:
    """Un projet TSI non publie est d'autant plus anormal qu'il est ancien ; annee inconnue = medium."""
    if year is None or year == current_year - 1:
        return "medium"
    return "high" if year <= current_year - 2 else "low"


def project_anomalies(p: dict, current_year: int) -> list[Anomaly]:
    """Anomalies d'un projet seul (front matter)."""
    if not p.get("detail_page"):
        if p.get("programme") == "TSI":
            return [_row(p, "tsi_not_published", _age_severity(p.get("year"), current_year),
                         f"no detail page (first seen {p.get('first_seen')})")]
        return [_row(p, "srsp_title_only", "info", "known from the programme PDF list only")]
    out = []
    if not p.get("deliverables"):
        out.append(_row(p, "published_no_deliverable", "medium" if p.get("results") == "achieved" else "low",
                        f"results: {p.get('results') or 'none'}"))
    if not p.get("results"):
        out.append(_row(p, "missing_results", "low", "no results section on the detail page"))
    return out


def load_projects(output_dir: Path) -> list[dict]:
    """Front matter de chaque projet (fichiers sans front matter ignores)."""
    projects = []
    for path in sorted((output_dir / "projects").glob("*.md")):
        try:
            front, _ = parse_front_matter(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if front and front.get("project_id"):
            projects.append(front)
    return projects


def load_publications(output_dir: Path) -> list[dict]:
    path = output_dir / "state" / "deliverables.json"
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("publications", {}).values())


def orphan_deliverables(publications: list[dict], known: set[str]) -> list[Anomaly]:
    """Publications dont aucun projet reference (id ou alias) n'existe."""
    out = []
    for pub in publications:
        ids = pub.get("project_ids") or []
        if any(i in known for i in ids):
            continue
        refs = f"references unknown {', '.join(ids)}" if ids else "references no project"
        out.append(Anomaly("", pub.get("title") or "", "", None, "", "orphan_deliverable", "medium",
                           f"{pub.get('publication_id')}: {refs}", pub.get("url") or ""))
    return out


def possible_duplicates(projects: list[dict]) -> list[Anomaly]:
    """Paires de projets d'un meme programme et d'un pays commun aux titres presque identiques."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in projects:
        for country in p.get("countries") or []:
            groups[(p.get("programme"), country)].append(p)
    seen: set[tuple[str, str]] = set()
    out = []
    for members in groups.values():
        for a, b in combinations(members, 2):
            first, second = sorted((a, b), key=lambda p: p["project_id"])
            key = (first["project_id"], second["project_id"])
            if key in seen:
                continue
            score = title_similarity(first.get("title") or "", second.get("title") or "")
            if score >= THRESHOLD:
                seen.add(key)
                out.append(_row(first, "possible_duplicate", "medium",
                                f"similar to {second['project_id']} ({second.get('year')}, score {score:.2f})"))
    return out


def detect(projects: list[dict], publications: list[dict], current_year: int) -> list[Anomaly]:
    known = {p["project_id"] for p in projects} | {a for p in projects for a in p.get("aliases") or []}
    rows = [a for p in projects for a in project_anomalies(p, current_year)]
    rows += orphan_deliverables(publications, known)
    rows += possible_duplicates(projects)
    return sorted(rows, key=lambda a: (SEVERITY_ORDER[a.severity], a.type, a.programme, a.year or 0, a.project_id))


def run(output_dir: Path, out_dir: Path, current_year: int | None = None) -> dict:
    raise NotImplementedError  # remplace a la tache 5
