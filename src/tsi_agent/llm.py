"""Fournisseur LLM des etapes de mise a jour : CLI Claude (defaut) ou API compatible OpenAI (EC GPT, OpenAI).

`make_json_generator` est la fabrique unique des 4 etapes a LLM (extraction des pages, resumes des livrables,
arbitrage du sitemap, liaison aux CSR). En mode `openai`, le JSON est demande en `response_format: json_object`,
avec le schema dans le prompt systeme ; les consommateurs valident la reponse avec les memes modeles pydantic.
`check_provider` et `ping` verifient le fournisseur avant tout travail (CLI).
"""

import json
import os
import re
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
JSON_INSTRUCTION = "Answer with a single JSON object."
PING_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
PING_MESSAGE = 'Return the JSON object {"ok": true}.'
PING_TIMEOUT_S = 60
_CONFIG_STATUSES = (401, 403, 404)
_CONFIG_NAMES = ("AuthenticationError", "PermissionDeniedError", "NotFoundError")
_QUOTA_CODES = ("insufficient_quota", "rate_limit_exceeded")


class LLMConfigError(LLMQuotaExceeded):
    """Cle, URL ou modele refuses par l'API : les lots s'arretent comme sur un quota, mais ce n'en est pas un."""


def _status(exc: BaseException) -> int | None:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def _is_config(exc: BaseException) -> bool:
    return type(exc).__name__ in _CONFIG_NAMES or _status(exc) in _CONFIG_STATUSES


def _is_quota(exc: BaseException) -> bool:
    return ("RateLimit" in type(exc).__name__ or _status(exc) == 429
            or getattr(exc, "code", None) in _QUOTA_CODES or "quota" in str(exc).lower())


def _config_error(message: str) -> LLMConfigError:
    return LLMConfigError(f"erreur de configuration du fournisseur LLM (cle, URL ou modele), pas un quota : {message}")


@component
class OpenAIJsonChatGenerator:
    """Chat generator a sortie JSON sur une API compatible OpenAI ; meme interface que ClaudeCliChatGenerator.

    Le client n'est cree qu'au premier appel : construire (ou exporter) un pipeline ne demande pas la cle.
    """

    def __init__(self, model: str = "gpt-5.1", api_base_url: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", timeout_s: int = 300,
                 json_schema: dict[str, Any] | None = None, max_retries: int = 2,
                 max_completion_tokens: int | None = None):
        self.model = model
        self.api_base_url = api_base_url or None
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self.json_schema = json_schema
        self.max_retries = max_retries
        self.max_completion_tokens = max_completion_tokens
        self._generator: OpenAIChatGenerator | None = None
        self._lock = threading.Lock()

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, model=self.model, api_base_url=self.api_base_url, api_key_env=self.api_key_env,
                               timeout_s=self.timeout_s, json_schema=self.json_schema, max_retries=self.max_retries,
                               max_completion_tokens=self.max_completion_tokens)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpenAIJsonChatGenerator":
        return default_from_dict(cls, data)

    def _openai(self) -> OpenAIChatGenerator:
        with self._lock:
            if self._generator is None:
                if not (os.environ.get(self.api_key_env) or "").strip():
                    raise _config_error(f"cle API absente, definir {self.api_key_env} (environnement ou .env)")
                kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}}
                if self.max_completion_tokens:
                    kwargs["max_completion_tokens"] = self.max_completion_tokens
                generator = OpenAIChatGenerator(
                    api_key=Secret.from_env_var(self.api_key_env), model=self.model, api_base_url=self.api_base_url,
                    timeout=float(self.timeout_s), max_retries=self.max_retries, generation_kwargs=kwargs)
                generator.warm_up()
                self._generator = generator
            return self._generator

    def _with_schema(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        instruction = (SCHEMA_INSTRUCTION.format(schema=json.dumps(self.json_schema, separators=(",", ":")))
                       if self.json_schema else JSON_INSTRUCTION)
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
            detail = f"{type(exc).__name__}: {str(exc)[:500]}"
            if _is_config(exc):
                raise _config_error(detail) from exc
            if _is_quota(exc):
                raise LLMQuotaExceeded(detail) from exc
            raise LLMCallError(detail) from exc
        replies = result.get("replies") or []
        if not replies:
            raise LLMCallError("OpenAI reply is empty")
        reply = replies[0]
        source = reply.meta or {}
        if source.get("finish_reason") == "length":
            raise LLMCallError("reponse tronquee (finish_reason=length) : augmenter openai_max_completion_tokens")
        usage = source.get("usage") or {}
        meta = {**source, "model": f"openai/{self.model}", "model_returned": source.get("model"),
                "usage": {**usage, "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                          "completion_tokens": int(usage.get("completion_tokens") or 0)}}
        return {"replies": [ChatMessage.from_assistant(reply.text or "", meta=meta)]}


def _openai_generator(settings: Settings, schema: dict[str, Any] | None, timeout_s: int) -> OpenAIJsonChatGenerator:
    return OpenAIJsonChatGenerator(model=settings.openai_model, api_base_url=settings.openai_base_url,
                                   api_key_env=settings.openai_api_key_env, timeout_s=timeout_s, json_schema=schema,
                                   max_retries=settings.openai_max_retries,
                                   max_completion_tokens=settings.openai_max_completion_tokens)


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
        return _openai_generator(settings, schema, max(timeout_s, settings.openai_timeout_s))
    raise ValueError(f"llm_provider inconnu : {provider!r} (attendu : {', '.join(PROVIDERS)})")


def check_provider(settings: Settings) -> None:
    """Verification sans appel : fournisseur connu et, en mode openai, cle presente ; sinon ValueError claire."""
    if settings.llm_provider not in PROVIDERS:
        raise ValueError(f"llm_provider inconnu : {settings.llm_provider!r} (attendu : {', '.join(PROVIDERS)}), "
                         "corriger le fichier de reglages")
    if settings.llm_provider == "openai" and not (os.environ.get(settings.openai_api_key_env) or "").strip():
        raise ValueError(f"cle API absente : la variable {settings.openai_api_key_env} est vide ou absente "
                         "(la mettre dans .env a la racine du depot, ou dans l'environnement)")


def ping(settings: Settings) -> dict[str, Any]:
    """Un tout petit appel JSON en mode openai : cle, URL et modele valides ? Leve LLMCallError (ou sous-classe)."""
    generator = _openai_generator(settings, PING_SCHEMA, PING_TIMEOUT_S)
    reply = generator.run(messages=[ChatMessage.from_system("You are a health check."),
                                    ChatMessage.from_user(PING_MESSAGE)])["replies"][0]
    text = reply.text or ""
    start, end = text.find("{"), text.rfind("}")
    try:
        ok = start >= 0 and json.loads(text[start:end + 1]).get("ok") is True
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        raise LLMCallError(f"reponse inattendue du modele au test de connexion : {text[:200]!r}")
    meta = reply.meta or {}
    return {"model": meta.get("model"), "model_returned": meta.get("model_returned")}


_INLINE_COMMENT = re.compile(r"\s+#.*$")


def read_dotenv(path: Path) -> dict[str, str]:
    """Lignes KEY=VALUE d'un fichier .env (BOM tolere, commentaires ignores, prefixe `export` tolere).

    Une valeur entre guillemets identiques est prise telle quelle (sans les guillemets) ; sinon un commentaire
    ` # ...` en fin de ligne est retire.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        close = value.find(value[0], 1) if value[:1] in ("'", '"') else -1
        if close > 0:
            value = value[1:close]  # KEY="v" # commentaire -> v
        else:
            value = _INLINE_COMMENT.sub("", value)
        values[key.strip().removeprefix("export ").strip()] = value
    return values


def load_dotenv(path: Path = Path(".env")) -> dict[str, str]:
    """Charge `.env` dans os.environ sans jamais ecraser une variable deja definie ; renvoie les variables ajoutees."""
    added: dict[str, str] = {}
    for key, value in read_dotenv(path).items():
        if key and key not in os.environ:
            os.environ[key] = value
            added[key] = value
    return added
