"""Recherche web locale par le CLI `claude` (outil natif WebSearch, souscription) ; meme interface que
SerperDevWebSearch (serperdev-haystack), qui la remplace dans deepset : run(query) -> documents + links."""

import json
import time
from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict
from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "snippet": {"type": "string"}},
        "required": ["title", "url", "snippet"]}}},
    "required": ["results"],
}
SEARCH_SYSTEM = ("You are a web search engine. Run the WebSearch tool for the user's query (you may refine it once "
                 "if the first results are poor), then return the results. Only return URLs that appeared in the "
                 "WebSearch results; never invent or guess a URL. For each result give its title and a 1-3 "
                 "sentence snippet saying what the page contains.")


@component
class ClaudeWebSearch:
    def __init__(self, model: str = "haiku", top_k: int = 8, timeout_s: int = 180, executable: str = "claude"):
        self.model = model
        self.top_k = top_k
        self.timeout_s = timeout_s
        self.executable = executable

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, model=self.model, top_k=self.top_k, timeout_s=self.timeout_s,
                               executable=self.executable)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClaudeWebSearch":
        return default_from_dict(cls, data)

    def _cli(self) -> Any:
        return ClaudeCliChatGenerator(model=self.model, timeout_s=self.timeout_s, json_schema=SEARCH_SCHEMA,
                                      executable=self.executable, allowed_tools=["WebSearch"])

    @component.output_types(documents=list[Document], links=list[str])
    def run(self, query: str) -> dict[str, Any]:
        """Search the web for the query and return documents (snippet + title/link meta) and the list of links."""
        t0 = time.monotonic()
        messages = [ChatMessage.from_system(SEARCH_SYSTEM),
                    ChatMessage.from_user(f"Query: {query}\nReturn up to {self.top_k} results.")]
        reply = self._cli().run(messages=messages)["replies"][0]
        meta = reply.meta or {}
        usage = meta.get("usage") or {}
        try:
            payload = json.loads(reply.text or "")
        except json.JSONDecodeError:
            payload = None
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):  # reponse illisible : aucun document, mais trace dans le journal
            runlog.event("ClaudeWebSearch", "web_search", query, "bad_format", answer=(reply.text or "")[:500])
            results = []
        documents: list[Document] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            documents.append(Document(content=str(item.get("snippet") or ""),
                                      meta={"title": str(item.get("title") or url), "link": url}))
            if len(documents) == self.top_k:
                break
        runlog.event("ClaudeWebSearch", "llm_call", str(meta.get("model")), model=meta.get("model"),
                     input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                     cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                     cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                     cost_usd=float(meta.get("cost_usd") or 0.0), query=query, results=len(documents),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"documents": documents, "links": [d.meta["link"] for d in documents]}
