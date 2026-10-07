from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    output_dir: str = "output"
    logs_dir: str = "logs"
    cache_dir: str = ".cache/http"
    user_agent: str = "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"
    min_interval_s: float = 2.0
    model: str = "sonnet"
    llm_timeout_s: int = 180
    max_concurrency: int = 2
    chunk_size: int = 25
    # Couche vectorisee (pipeline `index`).
    vectors_dir: str = "~/tsi-vectors"  # hors OneDrive : base Qdrant locale ; `~` developpe a l'usage
    embedding_dim: int = 384
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_batch_size: int = 32
    # $ par million de tokens, par préfixe de modèle ; sert quand le generator ne renvoie pas de coût (API OpenAI).
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Chatbot (pipeline `chat`).
    chat_model: str = "sonnet"
    chat_max_steps: int = 6
    chat_port: int = 8765
    # Etage livrables (pipeline `deliverables`).
    deliverables_dir: str = "output/deliverables"
    deliverables_max_words: int = 15000
    deliverables_max_pdf_mb: float = 50
    deliverables_recheck_days: int = 7  # relances : pas de reverification des publications recentes
    deliverables_llm_timeout_s: int = 600  # prompts longs, jusqu'a 15 000 mots
    # Analyse d'impact (onglet de la page web).
    impact_dir: str = "output/impact"
    impact_model: str = "opus"
    impact_max_steps: int = 20  # appels LLM maximum par analyse
    impact_max_searches: int = 10  # recherches web maximum par analyse (impose par l'outil)
    impact_page_words: int = 3000  # texte d'une page web transmis a l'agent
    impact_page_max_mb: float = 20  # taille maximale d'une page ou d'un document telecharge
    impact_project_chars: int = 20000  # texte du projet transmis a l'agent
    impact_llm_timeout_s: int = 600  # le rapport final est long
    web_search_model: str = "haiku"
    web_search_timeout_s: int = 180


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    p = Path(path)
    return Settings.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else Settings()


def load_sources(path: str | Path = "config/sources.yaml") -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
