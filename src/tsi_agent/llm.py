"""Fournisseur LLM des etapes de mise a jour : CLI Claude (defaut) ou API compatible OpenAI (EC GPT, OpenAI).

`make_json_generator` est la fabrique unique des 4 etapes a LLM (extraction des pages, resumes des livrables,
arbitrage du sitemap, liaison aux CSR). En mode `openai`, le JSON est demande en `response_format: json_object`,
avec le schema dans le prompt systeme ; les consommateurs valident la reponse avec les memes modeles pydantic.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any

from haystack import component, default_from_dict, default_to_dict
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage, ChatRole
from haystack.utils import Secret

from tsi_agent.components.claude_cli import ClaudeCliChatGenerator, LLMCallError, LLMQuotaExceeded
from tsi_agent.config import Settings

PROVIDERS = ("claude_cli", "openai")
SCHEMA_INSTRUCTION = "Answer with a single JSON object that conforms to this JSON schema: {schema}"


def _is_quota(exc: BaseException) -> bool:
    if "RateLimit" in type(exc).__name__:
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429


@component
class OpenAIJsonChatGenerator:
    """Chat generator a sortie JSON sur une API compatible OpenAI ; meme interface que ClaudeCliChatGenerator.

    Le client n'est cree qu'au premier appel : construire (ou exporter) un pipeline ne demande pas la cle.
    """

    def __init__(self, model: str = "gpt-5.1", api_base_url: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", timeout_s: int = 300,
                 json_schema: dict[str, Any] | None = None):
        self.model = model
        self.api_base_url = api_base_url or None
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self.json_schema = json_schema
        self._generator: OpenAIChatGenerator | None = None
        self._lock = threading.Lock()

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, model=self.model, api_base_url=self.api_base_url, api_key_env=self.api_key_env,
                               timeout_s=self.timeout_s, json_schema=self.json_schema)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpenAIJsonChatGenerator":
        return default_from_dict(cls, data)

    def _openai(self) -> OpenAIChatGenerator:
        with self._lock:
            if self._generator is None:
                if not os.environ.get(self.api_key_env):
                    raise LLMCallError(f"cle API absente : definir {self.api_key_env} (environnement ou .env)")
                generator = OpenAIChatGenerator(
                    api_key=Secret.from_env_var(self.api_key_env), model=self.model, api_base_url=self.api_base_url,
                    timeout=float(self.timeout_s), generation_kwargs={"response_format": {"type": "json_object"}})
                generator.warm_up()
                self._generator = generator
            return self._generator

    def _with_schema(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        if not self.json_schema:
            return messages
        instruction = SCHEMA_INSTRUCTION.format(schema=json.dumps(self.json_schema, separators=(",", ":")))
        out: list[ChatMessage] = []
        done = False
        for m in messages:
            if not done and m.is_from(ChatRole.SYSTEM):
                out.append(ChatMessage.from_system(f"{m.text or ''}\n\n{instruction}".strip()))
                done = True
            else:
                out.append(m)
        return out if done else [ChatMessage.from_system(instruction), *out]

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage]) -> dict[str, list[ChatMessage]]:
        generator = self._openai()
        try:
            result = generator.run(messages=self._with_schema(messages))
        except Exception as exc:
            if _is_quota(exc):
                raise LLMQuotaExceeded(str(exc)[:500]) from exc
            raise LLMCallError(f"{type(exc).__name__}: {str(exc)[:500]}") from exc
        replies = result.get("replies") or []
        if not replies:
            raise LLMCallError("OpenAI reply is empty")
        reply = replies[0]
        usage = (reply.meta or {}).get("usage") or {}
        meta = {**(reply.meta or {}), "model": f"openai/{self.model}",
                "usage": {**usage, "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                          "completion_tokens": int(usage.get("completion_tokens") or 0)}}
        return {"replies": [ChatMessage.from_assistant(reply.text or "", meta=meta)]}


def make_json_generator(settings: Settings, schema: dict[str, Any], *, timeout_s: int,
                        model: str | None = None) -> Any:
    """Chat generator a sortie JSON du fournisseur configure (`settings.llm_provider`).

    `model` (sonnet, opus...) ne vaut que pour le CLI Claude ; en mode openai, c'est `settings.openai_model`.
    En mode openai, le delai est au moins `settings.openai_timeout_s`.
    """
    provider = settings.llm_provider
    if provider == "claude_cli":
        return ClaudeCliChatGenerator(model=model or settings.model, json_schema=schema, timeout_s=timeout_s)
    if provider == "openai":
        return OpenAIJsonChatGenerator(model=settings.openai_model, api_base_url=settings.openai_base_url,
                                       api_key_env=settings.openai_api_key_env,
                                       timeout_s=max(timeout_s, settings.openai_timeout_s), json_schema=schema)
    raise ValueError(f"llm_provider inconnu : {provider!r} (attendu : {', '.join(PROVIDERS)})")


def read_dotenv(path: Path) -> dict[str, str]:
    """Lignes KEY=VALUE d'un fichier .env (commentaires ignores, guillemets retires, prefixe `export` tolere)."""
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


def load_dotenv(path: Path = Path(".env")) -> dict[str, str]:
    """Charge `.env` dans os.environ sans jamais ecraser une variable deja definie ; renvoie les variables ajoutees."""
    added: dict[str, str] = {}
    for key, value in read_dotenv(path).items():
        if key and key not in os.environ:
            os.environ[key] = value
            added[key] = value
    return added
