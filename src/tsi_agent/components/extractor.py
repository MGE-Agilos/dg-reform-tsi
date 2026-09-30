"""Extraction structurée d'une page projet par un chat generator Haystack injecté."""

import contextvars
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from haystack import component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ChatMessage
from haystack.utils import deserialize_chatgenerator_inplace
from pydantic import ValidationError

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.countries import COUNTRIES
from tsi_agent.models import DetailPage, ProjectContent, ProjectRecord

NAME = "ProjectExtractor"

SYSTEM_PROMPT = ("You restructure European Commission reform project pages into JSON. "
                 "You never add facts that are not in the page.")

USER_TEMPLATE = """You are given the text of a public project page of the European Commission \
(Technical Support Instrument or Structural Reform Support Programme).

Return a JSON object with exactly these keys:
- "summary": 3 to 5 sentences summarising the project, using only facts stated in the page.
- "context": the context / problem the project addresses, as clear prose.
- "support_delivered": the support the Commission delivered (activities, deliverables).
- "results_achieved": the results reported on the page.
- "countries": ISO 3166-1 alpha-2 codes of the EU Member States benefiting from the project \
(use "GR" for Greece); an empty list if the page does not say.

Rules:
- Use only information present in the page text.
- If the page does not cover a section, use exactly "Not reported on the source page."
- Write in English. Output only the JSON object, without code fences.

Project title: {title}
Source URL: {url}

--- PAGE TEXT START ---
{text}
--- PAGE TEXT END ---"""

RETRY_MESSAGE = ("Your previous answer could not be parsed ({error}). "
                 "Return only the JSON object with the keys summary, context, support_delivered, "
                 "results_achieved, countries.")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class ContentParseError(ValueError):
    pass


def parse_content(text: str) -> ProjectContent:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ContentParseError("no JSON object in reply")
    try:
        content = ProjectContent.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ContentParseError(str(exc).splitlines()[0]) from exc
    content.countries = [c.upper() for c in content.countries if c.upper() in COUNTRIES]
    return content


def _is_quota_error(exc: Exception) -> bool:
    return isinstance(exc, LLMQuotaExceeded) or "RateLimit" in type(exc).__name__


@component
class ProjectExtractor:
    def __init__(self, chat_generator: Any, max_concurrency: int = 2, max_retries: int = 1,
                 pricing: dict[str, dict[str, float]] | None = None):
        """
        :param chat_generator: tout chat generator Haystack (ClaudeCliChatGenerator, OpenAIChatGenerator...).
        :param pricing: prix en $ par million de tokens, par préfixe de modèle, utilisé quand le
            generator ne renvoie pas de coût (API OpenAI). Ex. {"gpt-4o-mini": {"input": 0.15, "output": 0.6}}.
        """
        self.chat_generator = chat_generator
        self.max_concurrency = max_concurrency
        self.max_retries = max_retries
        self.pricing = pricing or {}
        self._warmed = False

    def warm_up(self) -> None:
        if not self._warmed and hasattr(self.chat_generator, "warm_up"):
            self.chat_generator.warm_up()
        self._warmed = True

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, chat_generator=component_to_dict(obj=self.chat_generator, name="chat_generator"),
                               max_concurrency=self.max_concurrency, max_retries=self.max_retries,
                               pricing=self.pricing)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectExtractor":
        deserialize_chatgenerator_inplace(data["init_parameters"], key="chat_generator")
        return default_from_dict(cls, data)

    def _cost(self, meta: dict[str, Any]) -> float | None:
        if meta.get("cost_usd") is not None:
            return float(meta["cost_usd"])
        usage = meta.get("usage") or {}
        model = str(meta.get("model", ""))
        for prefix, price in sorted(self.pricing.items(), key=lambda kv: len(kv[0]), reverse=True):
            if model.startswith(prefix):
                return (usage.get("prompt_tokens", 0) * price.get("input", 0)
                        + usage.get("completion_tokens", 0) * price.get("output", 0)) / 1_000_000
        return None

    def _log_call(self, page: DetailPage, reply: ChatMessage, attempt: int, ms: int, result: str) -> None:
        meta = reply.meta or {}
        usage = meta.get("usage") or {}
        runlog.event(NAME, "llm_call", page.ref.project_id, result, duration_ms=ms, attempt=attempt,
                     model=meta.get("model"), input_tokens=usage.get("prompt_tokens", 0),
                     output_tokens=usage.get("completion_tokens", 0),
                     cache_read_tokens=usage.get("cache_read_input_tokens")
                     or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),  # format OpenAI
                     cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                     cost_usd=self._cost(meta))

    def _extract_one(self, page: DetailPage) -> ProjectRecord:
        messages = [ChatMessage.from_system(SYSTEM_PROMPT),
                    ChatMessage.from_user(USER_TEMPLATE.format(title=page.title, url=page.url, text=page.text))]
        error = "no attempt"
        for attempt in range(1, self.max_retries + 2):
            t0 = time.monotonic()
            reply = self.chat_generator.run(messages=messages)["replies"][0]
            ms = int((time.monotonic() - t0) * 1000)
            try:
                content = parse_content(reply.text or "")
            except ContentParseError as exc:
                error = str(exc)
                self._log_call(page, reply, attempt, ms, "invalid_json")
                messages = [*messages, reply, ChatMessage.from_user(RETRY_MESSAGE.format(error=error))]
                continue
            self._log_call(page, reply, attempt, ms, "ok")
            ref = page.ref.model_copy(update={
                "title": page.title or page.ref.title,
                "programme": page.programme or page.ref.programme,
                "year": page.year or page.ref.year,
                "countries": page.ref.countries or content.countries,
                "detail_url": page.url,
            })
            model = str((reply.meta or {}).get("model", "unknown"))
            return ProjectRecord(ref=ref, content=content, content_hash=page.content_hash, model=model)
        raise ContentParseError(error)

    @component.output_types(records=list[ProjectRecord], failed=list[str], quota_exceeded=bool)
    def run(self, pages: list[DetailPage]) -> dict[str, Any]:
        self.warm_up()
        stop = threading.Event()
        results: dict[int, ProjectRecord] = {}
        failed: list[str] = []

        def work(i: int, page: DetailPage) -> None:
            if stop.is_set():
                runlog.event(NAME, "error", page.ref.project_id, "skipped_quota")
                return
            try:
                results[i] = self._extract_one(page)
            except Exception as exc:
                if _is_quota_error(exc):
                    stop.set()
                    runlog.event(NAME, "error", page.ref.project_id, "quota_exceeded", error=str(exc)[:300])
                else:
                    runlog.event(NAME, "error", page.ref.project_id, "extract_failed", error=str(exc)[:300])
                failed.append(page.ref.project_id)

        with ThreadPoolExecutor(max_workers=max(1, self.max_concurrency)) as pool:
            for i, page in enumerate(pages):
                pool.submit(contextvars.copy_context().run, work, i, page)
        records = [results[i] for i in sorted(results)]
        return {"records": records, "failed": failed, "quota_exceeded": stop.is_set()}
