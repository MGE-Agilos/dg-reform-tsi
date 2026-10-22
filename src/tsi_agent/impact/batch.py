"""Analyses d'impact en lot (`impact-batch`) : selection de projets du store local, puis une analyse a la fois avec
le service existant (memes prompts, memes modeles, CLI Claude de la souscription).

Reprise : un projet qui a deja un rapport (output/impact/<project_id>.md) est saute, sauf `redo`. Sur quota, le lot
s'arrete proprement (les rapports deja ecrits restent) ; la relance reprend avec les projets restants. Un budget de
temps optionnel arrete le lot entre deux analyses (jamais au milieu d'une analyse).
"""

import time
from pathlib import Path
from typing import Any, Callable

from tsi_agent.chat.service import find_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.countries import COUNTRIES, iso_from_name
from tsi_agent.impact.catalog import FIELDS
from tsi_agent.impact.report import valid_project_id

RESULTS = ("achieved", "expected")
DEFAULT_LIMIT = 10


def country_iso(value: str) -> str | None:
    """Code ISO2 d'un pays donne par son code (LT) ou son nom (Lithuania) ; None si inconnu."""
    text = (value or "").strip()
    if text.upper() in COUNTRIES:
        return text.upper()
    return iso_from_name(text)


def has_report(impact_dir: str | Path, project_id: str) -> bool:
    return (Path(impact_dir) / f"{project_id}.md").exists()


def order_key(project: dict[str, Any]) -> tuple:
    """Projets lies a une CSR d'abord, resultats atteints d'abord, plus recents d'abord, puis id."""
    return (not project.get("csrs"), project.get("results") != "achieved", -(project.get("year") or 0),
            str(project.get("project_id")))


def select_projects(projects: list[dict[str, Any]], *, country: str | None = None, policy_area: str | None = None,
                    programme: str | None = None, results: str | None = None, year_from: int | None = None,
                    year_to: int | None = None, linked_only: bool = False,
                    ids: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """(projets retenus dans l'ordre de traitement, ids demandes inconnus). `country` : code ISO2 deja resolu.

    Avec `ids` : ordre des ids donnes, filtres appliques quand meme ; sans : ordre de `order_key`."""
    def keep(p: dict[str, Any]) -> bool:
        if country and country not in (p.get("countries") or []):
            return False
        if policy_area and str(p.get("policy_area") or "").lower() != policy_area.lower():
            return False
        if programme and str(p.get("programme") or "").upper() != programme.upper():
            return False
        if results and p.get("results") != results:
            return False
        year = p.get("year")
        if year_from is not None and (year is None or year < year_from):
            return False
        if year_to is not None and (year is None or year > year_to):
            return False
        return not (linked_only and not p.get("csrs"))

    if ids:
        by_id = {p["project_id"]: p for p in projects}
        wanted = list(dict.fromkeys(ids))
        return [by_id[i] for i in wanted if i in by_id and keep(by_id[i])], [i for i in wanted if i not in by_id]
    return sorted((p for p in projects if keep(p)), key=order_key), []


def project_meta(project: dict[str, Any]) -> dict[str, Any]:
    """Metadonnees transmises au rapport (memes champs que le catalogue de la page)."""
    return {k: project.get(k) for k in FIELDS}


def run_batch(projects: list[dict[str, Any]], runner: Callable[[str, dict[str, Any]], dict[str, Any]], *,
              impact_dir: str | Path, limit: int | None = DEFAULT_LIMIT, redo: bool = False,
              time_budget_s: float | None = None, log: Callable[[str], None] = print,
              clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Analyse sequentiellement les projets (deja selectionnes et ordonnes) ; `runner(project_id, meta)` lance une
    analyse (service.analyse) et renvoie son resultat.

    Saute les projets qui ont deja un rapport (sauf `redo`) ; `limit` borne le nombre d'analyses lancees.
    Resume : done (analyses terminees), skipped (rapport existant), failed ({project_id, error}), remaining (projets
    selectionnes non traites), quota_exceeded, time_budget_reached, cost_usd, duration_s."""
    t0 = clock()
    summary: dict[str, Any] = {"selected": len(projects), "done": [], "skipped": [], "failed": [], "remaining": [],
                               "quota_exceeded": False, "time_budget_reached": False, "cost_usd": 0.0}
    todo: list[dict[str, Any]] = []
    for project in projects:
        pid = project["project_id"]
        if not redo and has_report(impact_dir, pid):
            summary["skipped"].append(pid)
        elif not valid_project_id(pid):
            summary["failed"].append({"project_id": pid, "error": "invalid project id"})
        else:
            todo.append(project)
    cap = len(todo) if limit is None else max(limit, 0)
    started = 0
    for i, project in enumerate(todo):
        pid = project["project_id"]
        stop = summary["quota_exceeded"] or started >= cap
        if not stop and time_budget_s is not None and clock() - t0 >= time_budget_s:
            summary["time_budget_reached"] = True
            stop = True
        if stop:
            summary["remaining"].extend(p["project_id"] for p in todo[i:])
            break
        started += 1
        log(f"[{started}/{min(cap, len(todo))}] {pid} ...")
        t1 = clock()
        try:
            result = runner(pid, project_meta(project))
        except Exception as exc:  # quota : arret du lot ; autre erreur : projet note en echec, le lot continue
            if find_cause(exc, LLMQuotaExceeded):
                summary["quota_exceeded"] = True
                summary["remaining"].extend(p["project_id"] for p in todo[i:])
                log("    quota de la souscription Claude atteint : arret (relancer plus tard reprend)")
                break
            summary["failed"].append({"project_id": pid, "error": f"{type(exc).__name__}: {exc}"[:300]})
            log(f"    ECHEC ({round(clock() - t1)} s) : {type(exc).__name__}: {str(exc)[:200]}")
            continue
        front = result.get("front") or {}
        cost = float(result.get("cost_usd") or 0.0)
        summary["cost_usd"] += cost
        summary["done"].append(pid)
        score = front.get("impact_score")
        log(f"    OK ({round(clock() - t1)} s, {cost:.2f} $) score {score if score is not None else '-'}/10 "
            f"{front.get('impact_level') or ''}".rstrip())
    summary["cost_usd"] = round(summary["cost_usd"], 4)
    summary["duration_s"] = round(clock() - t0, 1)
    return summary


def exit_code(summary: dict[str, Any]) -> int:
    """1 quota (prioritaire), 124 budget de temps atteint, 0 sinon (les echecs individuels sont dans le resume)."""
    if summary.get("quota_exceeded"):
        return 1
    if summary.get("time_budget_reached"):
        return 124
    return 0
