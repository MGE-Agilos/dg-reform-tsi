"""Serveur web local du chatbot (Flask, 127.0.0.1 uniquement). L'historique vit dans la page."""

import logging
import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from tsi_agent.chat.pipeline import build_chat_pipeline, default_text_embedder, open_vector_store
from tsi_agent.chat.service import ask, find_cause, root_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.impact.catalog import load_catalog, search_catalog
from tsi_agent.impact.pipeline import build_impact_pipeline
from tsi_agent.impact.report import list_reports, read_report
from tsi_agent.impact.service import ImpactJobs, JobRunning, analyse, default_report_formatter

INDEX_HTML = Path(__file__).parent / "static" / "index.html"
QUOTA_MESSAGE = "The Claude subscription limit has been reached. Please try again later."
MAX_CONTENT_LENGTH = 1_000_000


def _valid_history(history: Any) -> bool:
    """Chaque tour doit etre {"role": "user"|"assistant", "text": str} ; sinon l'historique est invalide."""
    if not isinstance(history, list):
        return False
    for turn in history:
        if not isinstance(turn, dict):
            return False
        if turn.get("role") not in ("user", "assistant"):
            return False
        if not isinstance(turn.get("text"), str):
            return False
    return True


def create_app(settings: Settings, pipeline: Any = None, policy_areas: list[str] | None = None,
               impact_pipeline: Any = None, catalog: list[dict[str, Any]] | None = None,
               report_formatter: Any = None) -> Flask:
    """Construit l'application ; les pipelines (store + modele) sont charges une seule fois, ici.

    Production (`pipeline` absent) : un seul embedder e5 et un seul store Qdrant local, partages par le chat et
    l'analyse d'impact. Tests (`pipeline` fourni) : rien n'est construit ; l'analyse d'impact n'est active que si
    `impact_pipeline` est fourni et le catalogue vaut `catalog or []` (pas de lecture de output/)."""
    if pipeline is None:
        emb = default_text_embedder(settings)
        store = open_vector_store(settings)  # store local : un seul client Qdrant par dossier
        pipeline = build_chat_pipeline(settings, policy_areas or [], text_embedder=emb, document_store=store)
        if impact_pipeline is None:
            impact_pipeline = build_impact_pipeline(settings, text_embedder=emb, document_store=store)
        if catalog is None:
            catalog = load_catalog(Path(settings.output_dir) / "projects")
        if report_formatter is None:
            report_formatter = default_report_formatter(settings)
    catalog = catalog or []
    pipeline.warm_up()
    if impact_pipeline is not None:
        impact_pipeline.warm_up()
    by_id = {p["project_id"]: p for p in catalog}
    lock = threading.Lock()  # une question a la fois : un seul CLI claude en parallele
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

    @app.get("/")
    def index() -> Response:
        return Response(INDEX_HTML.read_text(encoding="utf-8"), mimetype="text/html")

    @app.post("/api/chat")
    def chat() -> Any:
        data = request.get_json(silent=True) or {}
        question = str(data.get("question") or "").strip()
        history = data.get("history") or []
        if not question:
            return jsonify({"error": "empty question"}), 400
        if not _valid_history(history):
            return jsonify({"error": "invalid history"}), 400
        with lock:
            try:
                return jsonify(ask(pipeline, question, history, logs_dir=settings.logs_dir))
            except Exception as exc:  # erreur generique renvoyee a la page ; le detail complet va dans les logs
                if find_cause(exc, LLMQuotaExceeded):
                    return jsonify({"error": QUOTA_MESSAGE}), 429
                logging.getLogger("tsi_agent").exception("Erreur lors du traitement d'une question du chatbot")
                cause = root_cause(exc)
                return jsonify({"error": f"Internal error ({type(cause).__name__}). Details in logs/runs/."}), 500

    def run_impact(project_id: str, on_step: Any) -> dict[str, Any]:
        return analyse(impact_pipeline, project_id, settings=settings, project_meta=by_id[project_id],
                       on_step=on_step, formatter=report_formatter)

    jobs = ImpactJobs(run_impact)
    app.config["IMPACT_JOBS"] = jobs

    def unavailable() -> Any:
        return jsonify({"error": "impact analysis not available"}), 503

    @app.get("/api/projects")
    def projects() -> Any:
        return jsonify({"projects": search_catalog(catalog, request.args.get("q", ""))})

    @app.post("/api/impact")
    def impact_start() -> Any:
        if impact_pipeline is None:
            return unavailable()
        project_id = str((request.get_json(silent=True) or {}).get("project_id") or "")
        if project_id not in by_id:
            return jsonify({"error": "unknown project"}), 404
        try:
            jobs.start(project_id)
        except JobRunning:
            return jsonify({"error": "an analysis is already running"}), 409
        return jsonify(jobs.status()), 202

    @app.get("/api/impact/job")
    def impact_job() -> Any:
        if impact_pipeline is None:
            return unavailable()
        return jsonify(jobs.status())

    @app.get("/api/impact/reports")
    def impact_reports() -> Any:
        return jsonify({"reports": list_reports(settings.impact_dir)})

    @app.get("/api/impact/reports/<project_id>")
    def impact_report(project_id: str) -> Any:
        found = read_report(settings.impact_dir, project_id)
        if found is None:
            return jsonify({"error": "no report"}), 404
        return jsonify({"front": found[0], "body": found[1]})

    return app
