"""Chat generator qui journalise chaque appel LLM (tokens, cout, duree) autour d'un generator Haystack."""

import inspect
import time
from typing import Any

from haystack import component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ChatMessage
from haystack.utils import deserialize_chatgenerator_inplace

from tsi_agent import runlog


@component
class LoggedChatGenerator:
    def __init__(self, chat_generator: Any, pricing: dict[str, dict[str, float]] | None = None):
        """:param pricing: $ par million de tokens par prefixe de modele, quand le generator ne donne pas de cout."""
        self.chat_generator = chat_generator
        self.pricing = pricing or {}

    def warm_up(self) -> None:
        if hasattr(self.chat_generator, "warm_up"):
            self.chat_generator.warm_up()

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, pricing=self.pricing,
                               chat_generator=component_to_dict(obj=self.chat_generator, name="chat_generator"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoggedChatGenerator":
        deserialize_chatgenerator_inplace(data["init_parameters"], key="chat_generator")
        return default_from_dict(cls, data)

    def _cost(self, meta: dict[str, Any]) -> float:
        if meta.get("cost_usd") is not None:
            return float(meta["cost_usd"])
        usage = meta.get("usage") or {}
        model = str(meta.get("model") or "")
        for prefix, price in sorted(self.pricing.items(), key=lambda kv: len(kv[0]), reverse=True):
            if model.startswith(prefix):
                return (usage.get("prompt_tokens", 0) * price.get("input", 0)
                        + usage.get("completion_tokens", 0) * price.get("output", 0)) / 1_000_000
        return 0.0

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
        accepted = inspect.signature(self.chat_generator.run).parameters
        extra = {k: v for k, v in kwargs.items() if k in accepted and v is not None}
        if tools is not None and "tools" in accepted:
            extra["tools"] = tools
        t0 = time.monotonic()
        result = self.chat_generator.run(messages=messages, **extra)
        reply = result["replies"][0]
        meta = reply.meta or {}
        usage = meta.get("usage") or {}
        runlog.event("ChatAgent", "llm_call", str(meta.get("model")), model=meta.get("model"),
                     input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                     cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                     cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                     cost_usd=self._cost(meta), duration_ms=round((time.monotonic() - t0) * 1000),
                     tool_calls=[c.tool_name for c in reply.tool_calls])
        return result
