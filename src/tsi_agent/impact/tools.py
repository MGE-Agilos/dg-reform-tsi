"""Outils web de l'agent d'analyse d'impact : recherche web et lecture d'une page (HTML, PDF, Word, texte).

Les sources sont numerotees a la suite de l'etat `sources` de l'Agent (comme le chatbot) ; une URL deja connue
garde son numero.
"""

import io
import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import pdfplumber
from bs4 import BeautifulSoup
from haystack import component, default_from_dict, default_to_dict
from haystack.components.converters import HTMLToDocument
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ByteStream
from haystack.utils import deserialize_component_inplace

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.deliverables import docx_text

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


def _resolve(host: str) -> list[str]:
    """Adresses IP d'un nom d'hote (remplace dans les tests : aucun acces reseau)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def check_public_url(url: str) -> None:
    """ValueError si l'URL n'est pas http(s) ou vise une adresse non publique (boucle locale, reseau prive,
    lien local, reservee, multicast) : l'agent ne doit pas pouvoir lire le poste ou le reseau interne."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"URL not allowed (only public http/https): {url}")
    host = parsed.hostname
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = _resolve(host)
        except OSError as exc:
            raise ValueError(f"cannot resolve host {host}: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            raise ValueError(f"URL not allowed (non-public address {ip}): {url}")


@component
class BoundedLinkFetcher:
    """Telechargement borne d'une page : taille maximale, delai global, redirections suivies a la main avec
    controle de l'adresse a chaque saut. Meme interface que LinkContentFetcher (qui peut le remplacer en YAML)."""

    def __init__(self, max_mb: float = 20, timeout_s: float = 20, user_agent: str = USER_AGENT,
                 max_redirects: int = 5):
        self.max_mb = max_mb
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self.max_redirects = max_redirects
        self._transport: Any = None  # transport httpx de test (non serialise)

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, max_mb=self.max_mb, timeout_s=self.timeout_s, user_agent=self.user_agent,
                               max_redirects=self.max_redirects)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BoundedLinkFetcher":
        return default_from_dict(cls, data)

    def _fetch(self, client: httpx.Client, url: str) -> ByteStream:
        max_bytes = int(self.max_mb * 1024 * 1024)
        deadline = time.monotonic() + self.timeout_s
        for _ in range(self.max_redirects + 1):
            check_public_url(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {self.timeout_s} s")
            with client.stream("GET", url, timeout=remaining) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = urljoin(str(resp.url), resp.headers["location"])
                    continue
                resp.raise_for_status()
                if int(resp.headers.get("content-length") or 0) > max_bytes:
                    raise ValueError(f"document larger than {self.max_mb} MB")
                data = bytearray()
                for chunk in resp.iter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError(f"document larger than {self.max_mb} MB")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"timed out after {self.timeout_s} s")
                ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip()
                return ByteStream(data=bytes(data), meta={"url": url, "content_type": ctype})
        raise ValueError(f"too many redirects (more than {self.max_redirects})")

    @component.output_types(streams=list[ByteStream])
    def run(self, urls: list[str]) -> dict[str, Any]:
        """Download each URL (public http/https only) within the size and time limits."""
        with httpx.Client(follow_redirects=False, transport=self._transport,
                          headers={"User-Agent": self.user_agent}) as client:
            return {"streams": [self._fetch(client, url) for url in urls]}


def _pdf_text(data: bytes, max_words: int | None) -> str:
    """Texte d'un PDF page par page (marqueurs "--- page N ---") ; s'arrete des que max_words mots sont lus."""
    parts: list[str] = []
    words = 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            parts.append(f"--- page {i} ---\n{text}")
            words += len(text.split())
            if max_words is not None and words >= max_words:
                break
    return "\n\n".join(parts)


def page_text(data: bytes, content_type: str, url: str, max_words: int | None = None) -> str:
    """Texte d'une page web ou d'un document ; ValueError si le type n'est pas pris en charge. `max_words` arrete
    l'extraction d'un PDF une fois ce nombre de mots atteint (les longs rapports ne sont pas lus en entier)."""
    ctype = (content_type or "").lower()
    if "pdf" in ctype or data[:5] == b"%PDF-":
        return _pdf_text(data, max_words)
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
        """:param fetcher: composant run(urls) -> {"streams": [...]} ; BoundedLinkFetcher par defaut
            (LinkContentFetcher dans deepset)."""
        self.max_words = max_words
        self.timeout_s = timeout_s
        self.fetcher = fetcher if fetcher is not None else BoundedLinkFetcher(timeout_s=timeout_s)

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
            text = page_text(stream.data, str(stream.meta.get("content_type") or ""), url,
                             max_words=self.max_words)
        except Exception as exc:  # erreur renvoyee a l'agent, qui continue
            runlog.event("PageReader", "tool_call", "fetch_page", "error", url=url, error=repr(exc)[:300])
            return {"result": f"Could not read {url}: {exc}", "sources": []}
        words = text.split()
        body = " ".join(words[: self.max_words])
        if len(words) > self.max_words:
            body += f"\n(truncated to the first {self.max_words} words)"
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
