"""Serveur web local du chatbot (Flask, 127.0.0.1 uniquement). L'historique vit dans la page."""

import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.service import ask, find_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings

INDEX_HTML = Path(__file__).parent / "static" / "index.html"
QUOTA_MESSAGE = "La limite de la souscription Claude est atteinte. Réessayez plus tard."


def create_app(settings: Settings, pipeline: Any = None, policy_areas: list[str] | None = None) -> Flask:
    """Construit l'application ; le pipeline (store + modele) est charge une seule fois, ici."""
    if pipeline is None:
        pipeline = build_chat_pipeline(settings, policy_areas or [])
    pipeline.warm_up()
    lock = threading.Lock()  # une question a la fois : un seul CLI claude en parallele
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        return Response(INDEX_HTML.read_text(encoding="utf-8"), mimetype="text/html")

    @app.post("/api/chat")
    def chat() -> Any:
        data = request.get_json(silent=True) or {}
        question = str(data.get("question") or "").strip()
        history = data.get("history") or []
        if not question or not isinstance(history, list):
            return jsonify({"error": "question vide ou historique invalide"}), 400
        with lock:
            try:
                return jsonify(ask(pipeline, question, history, logs_dir=settings.logs_dir))
            except Exception as exc:  # erreur renvoyee a la page ; le detail est dans le journal du run
                if find_cause(exc, LLMQuotaExceeded):
                    return jsonify({"error": QUOTA_MESSAGE}), 429
                return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return app
