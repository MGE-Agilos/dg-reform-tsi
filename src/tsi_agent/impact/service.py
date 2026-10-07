"""Une analyse d'impact = un run journalise : Agent -> JSON -> rapport Markdown enregistre. Les tâches tournent en
arriere-plan, une a la fois ; la progression vient des evenements du journal (ecouteur)."""

import threading
from datetime import date
from typing import Any, Callable
from urllib.parse import urlparse

from haystack import Pipeline
from haystack.dataclasses import ChatMessage, ChatRole

from tsi_agent import runlog
from tsi_agent.chat.service import find_cause, root_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.impact.prompt import user_prompt
from tsi_agent.impact.report import ReportFormatError, parse_report, read_report, render_report, write_report

QUOTA_MESSAGE = "The Claude subscription limit has been reached. Please try again later."


class AnalysisFailed(RuntimeError):
    pass


class JobRunning(RuntimeError):
    pass


def progress_text(record: dict[str, Any]) -> str | None:
    """Libelle de progression d'un evenement du journal ; None si l'evenement n'est pas affiche."""
    action, target = record.get("action"), record.get("target")
    if action == "llm_call":
        return "Analysing…" if record.get("component") == "ChatAgent" else None
    if action != "tool_call":
        return None
    result = record.get("result") or "ok"
    netloc = urlparse(str(record.get("url") or "")).netloc or record.get("url")
    if result != "ok":  # echec d'un outil : affiche tel quel (l'agent continue)
        if target == "web_search":
            return "Search limit reached" if result == "limit" else f"Web search failed: {record.get('query')}"
        if target == "fetch_page":
            return f"Could not read: {netloc}"
        if target == "get_project":
            return f"Project not found: {record.get('doc_id')}"
        return f"{target}: {result}"
    if target == "get_project":
        return f"Reading project {record.get('doc_id')}"
    if target == "search_deliverables":
        return f"Searching deliverables: {record.get('query')}"
    if target == "web_search":
        return f"Searching the web: {record.get('query')}"
    if target == "fetch_page":
        return f"Reading: {netloc}"
    return None


def analyse(pipeline: Pipeline, project_id: str, *, settings: Settings, project_meta: dict[str, Any],
            on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
    with runlog.open_run(settings.logs_dir) as run:
        if on_step is not None:
            def listener(record: dict[str, Any]) -> None:
                text = progress_text(record)
                if text:
                    on_step(text)
            run.listeners.append(listener)
        runlog.event("ImpactService", "impact_start", project_id)
        out = pipeline.run({"agent": {"messages": [ChatMessage.from_user(user_prompt(project_id))]}})["agent"]
        last = out.get("last_message")
        if last is None or not last.is_from(ChatRole.ASSISTANT) or last.tool_calls or not last.text:
            runlog.event("ImpactService", "impact_end", project_id, "step_limit", exit_reason=out.get("exit_reason"))
            raise AnalysisFailed("The analysis did not complete (step limit reached). Details in logs/runs/.")
        try:
            report = parse_report(last.text)
        except ReportFormatError as exc:
            runlog.event("ImpactService", "impact_end", project_id, "bad_format", error=str(exc),
                         answer=last.text[:2000])
            raise AnalysisFailed(f"The agent's final answer could not be read ({exc}).") from exc
        sources = out.get("sources") or []
        runlog.event("ImpactService", "impact_end", project_id, sources=len(sources),
                     searches=len(out.get("searches") or []))
        run.extra.update({"mode": "impact", "project_id": project_id})
        # Ecriture dans le run : l'alerte est journalisee et un echec d'ecriture marque le run en erreur.
        current = run.summary()
        markdown = render_report(project_meta, report, sources, {
            "run_id": run.run_id, "model": settings.impact_model, "cost_usd": current["cost_usd"],
            "duration_s": current["duration_s"], "generated": date.today().isoformat()})
        write_report(settings.impact_dir, project_id, markdown)
        front, body = read_report(settings.impact_dir, project_id)
        if front.get("removed_citations"):
            runlog.event("ImpactService", "alert", project_id,
                         message=f"citations inconnues retirees : {front['removed_citations']}")
    summary = run.final_summary
    return {"project_id": project_id, "front": front, "body": body, "run_id": run.run_id,
            "cost_usd": summary["cost_usd"], "duration_s": summary["duration_s"]}


class ImpactJobs:
    """Une analyse a la fois, en arriere-plan ; `status()` donne l'etat courant (pour la page)."""

    def __init__(self, runner: Callable[[str, Callable[[str], None]], dict[str, Any]]):
        self.runner = runner
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"state": "idle", "project_id": None, "steps": [], "result": None,
                                       "error": None}

    def start(self, project_id: str) -> None:
        with self._lock:
            if self._state["state"] == "running":
                raise JobRunning(self._state["project_id"])
            self._state = {"state": "running", "project_id": project_id, "steps": [], "result": None, "error": None}
            self._thread = threading.Thread(target=self._run, args=(project_id,), daemon=True)
            self._thread.start()

    def _step(self, text: str) -> None:
        with self._lock:
            self._state["steps"].append(text)

    def _run(self, project_id: str) -> None:
        try:
            result = self.runner(project_id, self._step)
            update = {"state": "done", "result": result}
        except Exception as exc:  # message lisible pour la page ; detail complet dans les logs
            if find_cause(exc, LLMQuotaExceeded):
                message = QUOTA_MESSAGE
            elif find_cause(exc, AnalysisFailed):
                message = str(find_cause(exc, AnalysisFailed))
            else:
                import logging
                logging.getLogger("tsi_agent").exception("Erreur lors d'une analyse d'impact")
                message = f"Internal error ({type(root_cause(exc)).__name__}). Details in logs/runs/."
            update = {"state": "error", "error": message}
        with self._lock:
            self._state.update(update)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "steps": list(self._state["steps"])}

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
