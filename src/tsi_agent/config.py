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
    # $ par million de tokens, par préfixe de modèle ; sert quand le generator ne renvoie pas de coût (API OpenAI).
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    p = Path(path)
    return Settings.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else Settings()


def load_sources(path: str | Path = "config/sources.yaml") -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
