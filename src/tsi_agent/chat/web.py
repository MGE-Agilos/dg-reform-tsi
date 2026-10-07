"""Serveur web local du chatbot (Flask, 127.0.0.1 uniquement). L'historique vit dans la page."""

import logging
import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.service import ask, find_cause, root_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings

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


def create_app(settings: Settings, pipeline: Any = None, policy_areas: list[str] | None = None) -> Flask:
    """Construit l'application ; le pipeline (store + modele) est charge une seule fois, ici."""
    if pipeline is None:
        pipeline = build_chat_pipeline(settings, policy_areas or [])
    pipeline.warm_up()
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

    return app
