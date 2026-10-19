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
    # Lecture du sitemap (une fois par run) : le site renvoie souvent des HTTP 429 sur ses pages.
    sitemap_retry_wait_s: float = 180  # sitemap incomplet : attente avant une nouvelle lecture complete
    sitemap_interval_s: float = 10  # entre deux requetes du sitemap (index, enfants)
    sitemap_attempts: int = 4  # tentatives par page du sitemap sur HTTP 429
    sitemap_backoff_s: list[float] = Field(default_factory=lambda: [60.0, 120.0, 240.0])  # Retry-After si plus long
    sitemap_budget_s: float = 1200  # toute la lecture (attentes comprises) ; ensuite copies anciennes
    # `deliverables` apres un `run` dont la lecture a echoue depuis moins de N s : une tentative rapide seulement.
    sitemap_failure_memory_s: float = 21600
    model: str = "sonnet"
    llm_timeout_s: int = 180
    max_concurrency: int = 2
    chunk_size: int = 25
    # Fournisseur LLM des etapes de mise a jour (run, deliverables, arbitrage du sitemap, csr-link) :
    # claude_cli (CLI Claude, souscription) ou openai (API compatible OpenAI : EC GPT, OpenAI).
    llm_provider: str = "claude_cli"
    openai_base_url: str | None = None  # ex. https://api.tech.ec.europa.eu/ecgpt/v1 ; vide : API OpenAI
    openai_api_key_env: str = "OPENAI_API_KEY"  # nom de la variable d'environnement (ou de .env) de la cle
    openai_model: str = "gpt-5.1"
    openai_timeout_s: int = 300  # delai minimal d'un appel en mode openai
    openai_max_retries: int = 2  # nouvelles tentatives du client OpenAI (erreurs reseau, 429, 5xx)
    openai_max_completion_tokens: int | None = None  # plafond de tokens de sortie (dont raisonnement) ; vide : aucun
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
    # Outil find_projects : liste de candidats (sans seuil de score) jugee par l'agent.
    finder_project_pool: int = 60  # projets gardes depuis la recherche sur les textes projet
    finder_project_chunks: int = 400  # morceaux projet lus avant regroupement par projet
    finder_deliverable_pool: int = 30  # projets gardes depuis la recherche sur les livrables
    finder_deliverable_chunks: int = 300  # morceaux livrable lus avant regroupement par projet
    finder_max_candidates: int = 150  # lignes de candidats listees au plus
    # Etage livrables (pipeline `deliverables`).
    deliverables_dir: str = "output/deliverables"
    deliverables_max_words: int = 15000
    deliverables_max_pdf_mb: float = 50
    deliverables_recheck_days: int = 7  # relances : pas de reverification des publications recentes
    deliverables_fetch_chunk: int = 25  # pages de publication lues par lot (enregistre avant le lot suivant)
    deliverables_llm_timeout_s: int = 600  # prompts longs, jusqu'a 15 000 mots
    # Analyse d'impact (onglet de la page web).
    impact_dir: str = "output/impact"
    impact_model: str = "opus"
    impact_score_model: str = "opus"  # score de 0 a 10 calcule a partir du rapport
    impact_max_steps: int = 20  # appels LLM maximum par analyse
    impact_max_searches: int = 10  # recherches web maximum par analyse (impose par l'outil)
    impact_page_words: int = 3000  # texte d'une page web transmis a l'agent
    impact_page_max_mb: float = 20  # taille maximale d'une page ou d'un document telecharge
    impact_project_chars: int = 20000  # texte du projet transmis a l'agent
    impact_llm_timeout_s: int = 600  # le rapport final est long
    web_search_model: str = "haiku"
    web_search_timeout_s: int = 180


def sitemap_options(settings: Settings) -> dict[str, Any]:
    """Parametres de lecture du sitemap de SourceListingFetcher (instantane dans le dossier de l'etat)."""
    return {"sitemap_retry_wait_s": settings.sitemap_retry_wait_s, "sitemap_interval_s": settings.sitemap_interval_s,
            "sitemap_attempts": settings.sitemap_attempts, "sitemap_backoff_s": list(settings.sitemap_backoff_s),
            "sitemap_budget_s": settings.sitemap_budget_s, "output_dir": settings.output_dir}


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    p = Path(path)
    return Settings.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else Settings()


def load_sources(path: str | Path = "config/sources.yaml") -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
