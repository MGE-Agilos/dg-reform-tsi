"""Outils web de l'agent d'analyse d'impact : recherche web et lecture d'une page (HTML, PDF, Word, texte).

Les sources sont numerotees a la suite de l'etat `sources` de l'Agent (comme le chatbot) ; une URL deja connue
garde son numero.
"""

import time
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from haystack import component, default_from_dict, default_to_dict
from haystack.components.converters import HTMLToDocument
from haystack.components.fetchers import LinkContentFetcher
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ByteStream
from haystack.utils import deserialize_component_inplace

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.deliverables import docx_text, pdf_text

USER_AGENT = "TSI-Impact-Agent/0.1 (DG REFORM project)"


def _known_ref(known: list[dict], url: str) -> dict | None:
    return next((s for s in known if s.get("url") == url), None)


@component
class WebSearchTool:
    def __init__(self, search_engine: Any, max_searches: int = 10):
        """:param search_engine: composant run(query) -> {"documents": [...]} (ClaudeWebSearch, SerperDevWebSearch)."""
        self.search_engine = search_engine
        self.max_searches = max_searches

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, max_searches=self.max_searches,
                               search_engine=component_to_dict(obj=self.search_engine, name="search_engine"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebSearchTool":
        deserialize_component_inplace(data["init_parameters"], key="search_engine")
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict], searches=list[str])
    def run(self, query: str, known_sources: list[dict] | None = None,
            past_searches: list[str] | None = None) -> dict[str, Any]:
        """Search the web (news, official sites, OECD/Commission reports, studies). Returns numbered results [n]
        with title, URL and snippet. Read a result with fetch_page before relying on it.

        :param query: Search query, preferably in English or in the country's language (e.g. "Croatia hospital
            arrears 2023 reform results").
        """
        known = known_sources or []
        if len(past_searches or []) >= self.max_searches:
            runlog.event("WebSearchTool", "tool_call", "web_search", "limit", query=query)
            return {"result": f"Search limit reached ({self.max_searches} searches). Use fetch_page on the results "
                              "you already have, then write the report.", "sources": [], "searches": []}
        t0 = time.monotonic()
        try:
            docs = self.search_engine.run(query=query)["documents"]
        except LLMQuotaExceeded:
            raise
        except Exception as exc:  # erreur renvoyee a l'agent, qui continue
            runlog.event("WebSearchTool", "tool_call", "web_search", "error", query=query, error=repr(exc)[:300])
            return {"result": f"Web search failed: {exc}", "sources": [], "searches": [query]}
        next_ref = len(known) + 1
        sources: list[dict[str, Any]] = []
        blocks: list[str] = []
        for doc in docs:
            url = doc.meta.get("link") or doc.meta.get("url")
            title = doc.meta.get("title") or url
            if not url:
                continue
            existing = _known_ref(known + sources, url)
            if existing:
                ref = existing["ref"]
            else:
                ref = next_ref
                next_ref += 1
                sources.append({"ref": ref, "kind": "web", "title": title, "url": url, "section": None,
                                "ai_generated": False})
            blocks.append(f"[{ref}] {title}\nURL: {url}\n{doc.content or ''}")
        runlog.event("WebSearchTool", "tool_call", "web_search", query=query, results=len(blocks),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": "\n\n".join(blocks) if blocks else "No results.", "sources": sources,
                "searches": [query]}


def page_text(data: bytes, content_type: str, url: str) -> str:
    """Texte d'une page web ou d'un document ; ValueError si le type n'est pas pris en charge."""
    ctype = (content_type or "").lower()
    if "pdf" in ctype or data[:5] == b"%PDF-":
        return pdf_text(data)[1]
    if "wordprocessingml" in ctype:
        return docx_text(data)[1]
    if "html" in ctype or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        stream = ByteStream(data=data, meta={"url": url, "content_type": "text/html"})
        docs = HTMLToDocument().run(sources=[stream])["documents"]
        text = docs[0].content if docs and docs[0].content else ""
        if len(text.split()) < 50:  # extraction trafilatura trop maigre : texte brut de la page
            text = BeautifulSoup(data, "html.parser").get_text(" ", strip=True)
        return text
    if ctype.startswith("text/"):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"unsupported content type: {content_type or 'unknown'}")


@component
class PageReader:
    def __init__(self, max_words: int = 3000, timeout_s: int = 20, fetcher: Any = None):
        self.max_words = max_words
        self.timeout_s = timeout_s
        self.fetcher = fetcher if fetcher is not None else LinkContentFetcher(
            raise_on_failure=True, timeout=timeout_s, user_agents=[USER_AGENT], retry_attempts=1)

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, max_words=self.max_words, timeout_s=self.timeout_s,
                               fetcher=component_to_dict(obj=self.fetcher, name="fetcher"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageReader":
        deserialize_component_inplace(data["init_parameters"], key="fetcher")
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict])
    def run(self, url: str, known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Read a web page or online document (HTML, PDF, Word) and return its text, to check what it says
        before citing it. Long pages are truncated.

        :param url: Full URL (http or https), e.g. one returned by web_search.
        """
        known = known_sources or []
        t0 = time.monotonic()
        try:
            if not url.startswith(("http://", "https://")):
                raise ValueError("not an http(s) URL")
            stream = self.fetcher.run(urls=[url])["streams"][0]
            text = page_text(stream.data, str(stream.meta.get("content_type") or ""), url)
        except Exception as exc:  # erreur renvoyee a l'agent, qui continue
            runlog.event("PageReader", "tool_call", "fetch_page", "error", url=url, error=repr(exc)[:300])
            return {"result": f"Could not read {url}: {exc}", "sources": []}
        words = text.split()
        body = " ".join(words[: self.max_words])
        if len(words) > self.max_words:
            body += f"\n(truncated: first {self.max_words} of {len(words)} words)"
        existing = _known_ref(known, url)
        sources: list[dict[str, Any]] = []
        if existing:
            ref, title = existing["ref"], existing.get("title") or url
        else:
            ref, title = len(known) + 1, urlparse(url).netloc + urlparse(url).path
            sources.append({"ref": ref, "kind": "web", "title": title, "url": url, "section": None,
                            "ai_generated": False})
        runlog.event("PageReader", "tool_call", "fetch_page", url=url, words=len(words),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": f"[{ref}] {title}\nURL: {url}\n{body}", "sources": sources}
