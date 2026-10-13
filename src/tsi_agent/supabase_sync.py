"""Synchronisation des donnees de l'explorateur web vers Supabase (PostgREST, cle service_role).

Remplacement complet : suppression des tables dans l'ordre des dependances, puis insertion par lots. La cle
service_role vient de l'environnement ou de `.env` (SUPABASE_URL, SUPABASE_SERVICE_KEY) : jamais d'un fichier
suivi par git. La table `testers` n'est jamais touchee.
"""

import json
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from tsi_agent.anomalies import detect, load_csr_links, load_projects, load_publications
from tsi_agent.chunking import INTRO, SUMMARY, split_sections
from tsi_agent.csr import build_csrs, read_rows
from tsi_agent.render import parse_front_matter

TABLES = ("projects", "csrs", "csr_parts", "project_csr_links", "anomalies")
# Suppression : liens avant les tables qu'ils referencent ; insertion dans l'ordre inverse.
DELETE_ORDER = ("project_csr_links", "csr_parts", "csrs", "projects", "anomalies")
INSERT_ORDER = ("projects", "csrs", "csr_parts", "project_csr_links", "anomalies")
# Filtre PostgREST couvrant toutes les lignes (DELETE sans filtre refuse) : colonne non nulle de chaque table.
DELETE_FILTER = {"projects": "project_id=not.is.null", "csrs": "csr_id=not.is.null",
                 "csr_parts": "part_id=not.is.null", "project_csr_links": "project_id=not.is.null",
                 "anomalies": "id=not.is.null"}
BATCH = 500
SUMMARY_CHARS = 1500


def _project_row(fm: dict, body: str) -> dict[str, Any]:
    sections = dict(split_sections(body))
    summary = sections.get(SUMMARY) or sections.get(INTRO) or None
    urls = fm.get("source_urls") or []
    csrs = fm.get("csrs")
    return {"project_id": fm["project_id"], "title": str(fm.get("title") or fm["project_id"]),
            "programme": fm.get("programme"), "year": fm.get("year") if isinstance(fm.get("year"), int) else None,
            "countries": list(fm.get("countries") or []), "policy_area": fm.get("policy_area"),
            "results": fm.get("results"), "detail_page": bool(fm.get("detail_page")),
            "url": urls[0] if urls else None, "summary": summary[:SUMMARY_CHARS] if summary else None,
            "deliverables": int(fm.get("deliverables") or 0), "csrs": list(csrs) if isinstance(csrs, list) else []}


def build_rows(output_dir: Path, current_year: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Lignes de chaque table, depuis output/ (sans reseau)."""
    projects = []
    for path in sorted((output_dir / "projects").glob("*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except ValueError:
            continue
        if isinstance(fm, dict) and fm.get("project_id"):
            projects.append(_project_row(fm, body))
    csr_rows, part_rows = [], []
    xlsx = output_dir / "csr" / "csr_full.xlsx"
    for csr in build_csrs(read_rows(xlsx)) if xlsx.exists() else []:
        latest = csr.latest_assessment
        csr_rows.append({"csr_id": csr.csr_id, "country": csr.country, "year": csr.year, "number": csr.number,
                         "title": csr.title, "text": csr.text, "policy_areas": csr.policy_areas,
                         "assessment": latest[1] if latest else None, "assessment_year": latest[0] if latest else None,
                         "source_url": "https://ec.europa.eu/economy_finance/country-specific-recommendations-database/"})
        for part in csr.subparts or ([csr.full] if csr.full else []):
            part_rows.append({"part_id": part.part_id, "csr_id": csr.csr_id, "country": csr.country,
                              "year": csr.year, "text": part.text, "policy_areas": part.policy_areas,
                              "assessment": part.assessment})
    project_ids = {p["project_id"] for p in projects}
    part_ids = {p["part_id"] for p in part_rows}
    links = []
    for path in sorted((output_dir / "csr_links").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("status") != "ok" or record.get("project_id") not in project_ids:
            continue
        for link in record.get("links") or []:
            if link.get("csr_id") in part_ids:
                links.append({"project_id": record["project_id"], "part_id": link["csr_id"],
                              "level": link.get("level"), "rationale": link.get("rationale") or "",
                              "basis": record.get("basis") or "full_text"})
    anomalies = [{**asdict(a), "project_id": a.project_id or None}
                 for a in detect(load_projects(output_dir), load_publications(output_dir),
                                 current_year or date.today().year, csr_links=load_csr_links(output_dir))]
    return {"projects": projects, "csrs": csr_rows, "csr_parts": part_rows, "project_csr_links": links,
            "anomalies": anomalies}
