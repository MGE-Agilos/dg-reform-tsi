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


class SyncError(RuntimeError):
    pass


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


def credentials(dotenv: Path = Path(".env")) -> tuple[str, str]:
    """(url, cle service_role) : variables d'environnement, sinon `.env` ; erreur claire si absentes."""
    file = _dotenv(dotenv)
    url = os.environ.get("SUPABASE_URL") or file.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or file.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SyncError("SUPABASE_URL et SUPABASE_SERVICE_KEY requis (environnement ou .env, jamais dans git)")
    return url.rstrip("/"), key


def _check(resp: httpx.Response, table: str, action: str) -> None:
    if resp.status_code >= 300:
        raise SyncError(f"{action} {table}: HTTP {resp.status_code} {resp.text[:500]}")


def push(rows: dict[str, list[dict[str, Any]]], url: str, key: str, *, client: httpx.Client | None = None,
         backup: Path | None = None) -> dict[str, int]:
    """Remplace le contenu des tables (sauf testers) ; sauvegarde JSON des lignes avant toute suppression."""
    if backup is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Prefer": "return=minimal"}
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        for table in DELETE_ORDER:
            _check(client.delete(f"{url}/rest/v1/{table}?{DELETE_FILTER[table]}", headers=headers), table, "delete")
        for table in INSERT_ORDER:
            data = rows.get(table) or []
            for start in range(0, len(data), BATCH):
                _check(client.post(f"{url}/rest/v1/{table}", headers=headers,
                                   content=json.dumps(data[start:start + BATCH], ensure_ascii=False)), table, "insert")
    finally:
        if own:
            client.close()
    return {table: len(rows.get(table) or []) for table in INSERT_ORDER}
