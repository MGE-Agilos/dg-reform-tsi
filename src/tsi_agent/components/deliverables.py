"""Etage livrables : publications liees aux projets, PDF, texte integral, resume IA, section des fichiers projet."""

import contextvars
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pdfplumber
from haystack import component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ChatMessage
from haystack.utils import deserialize_chatgenerator_inplace
from pydantic import ValidationError

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.deliverables_state import DeliverableEntry, DeliverableStore
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import slug_from_url
from tsi_agent.models import (DeliverableContent, DeliverableFileText, DeliverableRecord, DeliverableText,
                              Publication)
from tsi_agent.parsers.publication import files_key, parse_publication, publication_links, select_files
from tsi_agent.parsers.sitemap import not_modified_since, sitemap_lastmods
from tsi_agent.render import (deliverables_section, parse_front_matter, render, replace_deliverables_section,
                              with_deliverables_count)
from tsi_agent.store import ProjectStore, _retry_on_lock, file_stem, merge_publication_urls, shared_store


class _Http:
    """Acces partage au PoliteFetcher (un par processus) ; les tests injectent `_fetcher`."""

    cache_dir: str
    min_interval_s: float
    user_agent: str
    _fetcher: PoliteFetcher | None

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher


@component
class PublicationDiscoverer(_Http):
    """Liens de publication des pages detail, lues dans le cache HTTP (le run d'extraction vient de le rafraichir).

    Page detail absente du cache (non relue par un `run` incremental) : liens enregistres dans l'etat lors de sa
    derniere lecture, a defaut publications deja connues de ce projet. Chaque publication porte la date <lastmod>
    du sitemap de sa page (`sitemap_url`), lu une fois par instance.
    """

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT, sitemap_url: str | None = None,
                 sitemap_retry_wait_s: float = 180):
        self.output_dir = output_dir
        self.sitemap_retry_wait_s = sitemap_retry_wait_s
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.sitemap_url = sitemap_url
        self._fetcher = None
        self._store: ProjectStore | None = None
        self._lastmods: dict[str, str] | None = None

    def _sitemap_dates(self) -> dict[str, str]:
        """URL -> <lastmod> ({} sans sitemap configure, ou s'il est indisponible ou incomplet)."""
        if self._lastmods is None:
            self._lastmods = {}
            if self.sitemap_url:
                from tsi_agent.components.listing import SourceListingFetcher
                listing = SourceListingFetcher(sources={"sitemap_url": self.sitemap_url}, cache_dir=self.cache_dir,
                                               min_interval_s=self.min_interval_s, user_agent=self.user_agent,
                                               sitemap_retry_wait_s=self.sitemap_retry_wait_s)
                listing._fetcher = self._get_fetcher()
                for page in listing.fetch_sitemap():
                    self._lastmods.update(sitemap_lastmods(page.content))
                runlog.event("PublicationDiscoverer", "sitemap", self.sitemap_url,
                             "ok" if self._lastmods else "unavailable", dates=len(self._lastmods))
        return self._lastmods

    @component.output_types(publications=list[Publication], complete=bool)
    def run(self) -> dict[str, Any]:
        store = self._store if self._store is not None else shared_store(self.output_dir)
        fetcher = self._get_fetcher()
        lastmods = self._sitemap_dates()
        known_by_project: dict[str, list[str]] | None = None
        by_url: dict[str, Publication] = {}
        missing: list[str] = []
        projects = 0
        from_state = 0
        remembered = False
        for project_id, entry in sorted(store.all().items()):
            if not entry.detail_page or not entry.listed or not entry.source_urls:
                continue
            html = fetcher.cached(entry.source_urls[0])  # source_urls[0] = page detail (render.source_urls)
            if html is not None:
                links = merge_publication_urls(project_id, entry.publication_urls, publication_links(html)) or []
                if entry.publication_urls != links:
                    store.put(entry.model_copy(update={"publication_urls": links}), save=False)
                    remembered = True
            elif entry.publication_urls is not None:
                links, from_state = list(entry.publication_urls), from_state + 1
            else:
                if known_by_project is None:  # etat d'avant `publication_urls` : publications deja traitees
                    known_by_project = {}
                    for pub_entry in DeliverableStore(self.output_dir).all().values():
                        for pid in pub_entry.project_ids:
                            known_by_project.setdefault(pid, []).append(pub_entry.url)
                if project_id not in known_by_project:
                    missing.append(project_id)
                    continue
                links, from_state = sorted(known_by_project[project_id]), from_state + 1
            projects += bool(links)
            for url in links:
                pub = by_url.setdefault(url, Publication(publication_id=slug_from_url(url), url=url,
                                                         sitemap_lastmod=lastmods.get(url)))
                if project_id not in pub.project_ids:
                    pub.project_ids.append(project_id)
        if remembered:
            store.flush()
        if missing:
            runlog.event("PublicationDiscoverer", "alert", None, "warning", missing=missing[:20],
                         message=f"{len(missing)} detail page(s) not in the HTTP cache: run `tsi_agent run` first")
        publications = list(by_url.values())
        runlog.event("PublicationDiscoverer", "discover", None, publications=len(publications),
                     projects_with_publications=projects, missing_pages=len(missing), from_state=from_state)
        return {"publications": publications, "complete": not missing}


@component
class PublicationFetcher(_Http):
    """Pages de publication ; seules les publications nouvelles ou dont les fichiers ont change sont retraitees."""

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT, recheck_days: int = 7):
        """:param recheck_days: une publication traitee depuis moins de N jours n'est pas reverifiee (sauf full)."""
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.recheck_days = recheck_days
        self._fetcher = None
        self._offered: set[str] = set()  # deja proposees pendant ce run (boucle par lots du runner)

    @component.output_types(to_process=list[Publication], unchanged=list[Publication], failed=list[str],
                            remaining=int, skipped=int, undated=int, fetched=int)
    def run(self, publications: list[Publication], full: bool = False, limit: int | None = None,
            recheck_all: bool = False, fetch_limit: int | None = None) -> dict[str, Any]:
        """Publication connue dont la date du sitemap n'est pas plus recente que celle enregistree : non relue
        (`skipped`, avant `recheck_days` et les limites), sauf `full` ou `recheck_all`. `undated` : pages relues
        faute de date du sitemap. `limit` : publications envoyees au LLM ; `fetch_limit` : pages de publication lues
        (`fetched`) dans cet appel, le reste compte dans `remaining` pour l'appel suivant. Une page n'est lue qu'une
        fois par instance (par run), meme sous `recheck_all`."""
        known = DeliverableStore(self.output_dir)
        # Les relances rapprochees (reprise apres limite de souscription) ne reverifient pas les pages deja vues.
        recent_since = (date.today() - timedelta(days=self.recheck_days)).isoformat()
        to_process: list[Publication] = []
        unchanged: list[Publication] = []
        failed: list[str] = []
        remaining = skipped = undated = fetched = 0
        for pub in publications:
            if pub.url in self._offered:
                continue
            entry = known.get(pub.publication_id)
            # Non relue : la date transmise au writer est celle deja enregistree (aucune lecture ce run).
            kept = {"title": entry.title, "date": entry.date, "files_key": entry.files_key,
                    "sitemap_lastmod": entry.sitemap_lastmod} if entry is not None else {}
            if (entry is not None and not full and not recheck_all
                    and not_modified_since(pub.sitemap_lastmod, entry.sitemap_lastmod)):
                self._offered.add(pub.url)
                unchanged.append(pub.model_copy(update=kept))
                skipped += 1
                runlog.event("PublicationFetcher", "classify", pub.publication_id, "unchanged_sitemap",
                             lastmod=pub.sitemap_lastmod)
                continue
            if entry is not None and not full and entry.processed >= recent_since:
                self._offered.add(pub.url)
                unchanged.append(pub.model_copy(update=kept))
                runlog.event("PublicationFetcher", "classify", pub.publication_id, "recent")
                continue
            if ((limit is not None and len(to_process) >= limit)
                    or (fetch_limit is not None and fetched >= max(1, fetch_limit))):
                remaining += 1
                continue
            self._offered.add(pub.url)
            fetched += 1
            undated += pub.sitemap_lastmod is None
            try:
                parse = parse_publication(self._get_fetcher().get(pub.url).content)
            except FetchError as exc:
                runlog.event("PublicationFetcher", "alert", pub.publication_id, "warning",
                             message=f"publication page unavailable: {exc}")
                failed.append(pub.publication_id)
                continue
            files = select_files(parse)
            pub = pub.model_copy(update={"title": parse.title or pub.publication_id, "date": parse.date,
                                         "meta": parse.meta, "files": files, "files_key": files_key(files)})
            if not files:
                runlog.event("PublicationFetcher", "classify", pub.publication_id, "no_files")
                continue
            if entry is not None and entry.files_key == pub.files_key and not full:
                unchanged.append(pub)
                continue
            runlog.event("PublicationFetcher", "classify", pub.publication_id, "changed" if entry else "new",
                         files=len(files))
            to_process.append(pub)
        return {"to_process": to_process, "unchanged": unchanged, "failed": failed, "remaining": remaining,
                "skipped": skipped, "undated": undated, "fetched": fetched}


def pdf_text(data: bytes) -> tuple[int, str]:
    """(nombre de pages, texte) ; pages separees par un marqueur."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [f"--- page {i} ---\n{page.extract_text() or ''}" for i, page in enumerate(pdf.pages, start=1)]
    return len(pages), "\n\n".join(pages)


def docx_text(data: bytes) -> tuple[int, str]:
    """(0, texte) d'un document Word : paragraphes et tableaux dans l'ordre du document (pas de notion de page)."""
    from docx import Document as WordDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = WordDocument(io.BytesIO(data))
    parts: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, doc).text.strip()
            if text:
                parts.append(text)
        elif tag == "tbl":
            for row in Table(child, doc).rows:
                cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))  # cellules fusionnees
                if cells:
                    parts.append(" | ".join(cells))
    return 0, "\n\n".join(parts)


META_FIELDS = ("countries", "year", "programme", "policy_area")


def project_meta(project_ids: list[str], store: ProjectStore) -> dict[str, Any]:
    """Metadonnees des projets lies, reprises dans le texte integral : pays (union), plus petite annee, 1res valeurs."""
    countries: set[str] = set()
    years: list[int] = []
    programme = policy_area = None
    for project_id in project_ids:
        text = store.read_project_file(project_id)
        if text is None:
            continue
        try:
            fm, _ = parse_front_matter(text)
        except ValueError:
            continue
        if not isinstance(fm, dict):
            continue
        countries.update(str(c) for c in fm.get("countries") or [])
        if isinstance(fm.get("year"), int):
            years.append(fm["year"])
        programme = programme or fm.get("programme") or None
        policy_area = policy_area or fm.get("policy_area") or None
    return {"countries": sorted(countries), "year": min(years) if years else None, "programme": programme,
            "policy_area": policy_area}


@component
class DeliverableTextExtractor(_Http):
    """Telecharge les fichiers retenus (PDF, Word ; sans cache HTTP), extrait le texte, ecrit output/deliverables/<id>.md."""

    def __init__(self, output_dir: str = "output", deliverables_dir: str = "output/deliverables",
                 cache_dir: str = ".cache/http", min_interval_s: float = 2.0, user_agent: str = DEFAULT_USER_AGENT,
                 max_pdf_mb: float = 50):
        self.output_dir = output_dir
        self.deliverables_dir = deliverables_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.max_pdf_mb = max_pdf_mb
        self._fetcher = None
        self._store: ProjectStore | None = None

    def _alert(self, pub: Publication, url: str, reason: str) -> None:
        runlog.event("DeliverableTextExtractor", "alert", url, "warning",
                     message=f"{pub.publication_id}: file skipped ({reason})")

    def _read(self, pub: Publication, url: str) -> tuple[int, str] | None:
        try:
            data = self._get_fetcher().get(url, cache=False).content
        except FetchError as exc:
            self._alert(pub, url, str(exc))
            return None
        if len(data) > self.max_pdf_mb * 1024 * 1024:
            self._alert(pub, url, f"too large: {len(data) // 1024} KB")
            return None
        if data.startswith(b"%PDF"):
            reader, kind = pdf_text, "PDF"
        elif data.startswith(b"PK") and ".docx" in url.lower():
            reader, kind = docx_text, "DOCX"
        else:
            self._alert(pub, url, "unsupported format (neither PDF nor DOCX)")
            return None
        try:
            pages, text = reader(data)
        except Exception as exc:  # pdfplumber/python-docx levent des types varies sur un fichier abime
            self._alert(pub, url, f"unreadable {kind}: {type(exc).__name__}")
            return None
        runlog.event("DeliverableTextExtractor", "download", url, bytes=len(data), pages=pages,
                     words=len(text.split()))
        return pages, text

    def _write(self, pub: Publication, files: list[DeliverableFileText]) -> str:
        folder = Path(self.deliverables_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{file_stem(pub.publication_id)}.md"
        front = {"publication_id": pub.publication_id, "title": pub.title, "url": pub.url, "date": pub.date,
                 "projects": list(pub.project_ids),
                 "files": [{"document": f.file.document, "filename": f.file.filename, "language": f.file.language,
                            "url": f.file.url, "pages": f.pages, "words": f.words} for f in files]}
        store = self._store if self._store is not None else shared_store(self.output_dir)
        front.update(project_meta(list(pub.project_ids), store))
        body = f"# {pub.title}\n\n" + "\n\n".join(
            f"## {f.file.document or f.file.filename}\n\nSource: {f.file.url}\n\n{f.text}" for f in files)
        _retry_on_lock(lambda: path.write_text(render(front, body), encoding="utf-8"))
        try:
            return path.relative_to(Path(self.output_dir)).as_posix()
        except ValueError:
            return path.as_posix()

    @component.output_types(texts=list[DeliverableText], failed=list[str])
    def run(self, publications: list[Publication]) -> dict[str, Any]:
        texts: list[DeliverableText] = []
        failed: list[str] = []
        for pub in publications:
            files = []
            for f in pub.files:
                read = self._read(pub, f.url)
                if read is not None:
                    files.append(DeliverableFileText(file=f, pages=read[0], words=len(read[1].split()), text=read[1]))
            if not files:
                failed.append(pub.publication_id)  # retentee au prochain run (pas d'entree dans l'etat)
                continue
            texts.append(DeliverableText(publication=pub, files=files, full_text_path=self._write(pub, files)))
        return {"texts": texts, "failed": failed}


SUMMARY_SYSTEM = ("You summarise the deliverables of European Commission technical support projects into JSON. "
                  "You never add facts that are not in the documents.")
SUMMARY_TEMPLATE = """You are given the text of the deliverables (final report, presentation, factsheet...) of a \
European Commission technical support project (Technical Support Instrument or Structural Reform Support Programme).

Return a JSON object with exactly these keys:
- "summary": 4 to 8 sentences on what the project delivered and achieved, using only the documents.
- "key_outputs": up to 6 short items, the main outputs produced (strategies, analyses, tools, trainings...).
- "results": up to 6 short items, results achieved or main recommendations.
- "figures": up to 6 short items, key figures with their context (for example "40+ stakeholders involved").

Rules:
- Use only information present in the documents; never invent facts or figures.
- Do not name individual persons; organisations may be named.
- Write in English. Output only the JSON object, without code fences.

Publication: {title}
Source URL: {url}

--- DOCUMENTS START ---
{text}
--- DOCUMENTS END ---"""
SUMMARY_RETRY = ("Your previous answer could not be parsed ({error}). Return only the JSON object with the keys "
                 "summary, key_outputs, results, figures.")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_deliverable_content(text: str) -> DeliverableContent:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    try:
        return DeliverableContent.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc).splitlines()[0]) from exc


def documents_text(text: DeliverableText, max_words: int) -> tuple[str, bool]:
    """Texte des fichiers, dans l'ordre de la page, limite a `max_words` mots (debut des documents)."""
    parts: list[str] = []
    budget = max_words
    truncated = False
    for f in text.files:
        words = f.text.split()
        if budget <= 0:
            truncated = True
            break
        kept = words[:budget]
        truncated = truncated or len(kept) < len(words)
        budget -= len(kept)
        parts.append(f"### {f.file.document or f.file.filename}\n" + " ".join(kept))
    body = "\n\n".join(parts)
    if truncated:
        body += f"\n\n[... truncated after {max_words} words ...]"
    return body, truncated


@component
class DeliverableSummarizer:
    def __init__(self, chat_generator: Any, max_concurrency: int = 2, max_retries: int = 1,
                 pricing: dict[str, dict[str, float]] | None = None, max_words: int = 15000):
        self.chat_generator = chat_generator
        self.max_concurrency = max_concurrency
        self.max_retries = max_retries
        self.pricing = pricing or {}
        self.max_words = max_words
        self._warmed = False

    def warm_up(self) -> None:
        if not self._warmed and hasattr(self.chat_generator, "warm_up"):
            self.chat_generator.warm_up()
        self._warmed = True

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, chat_generator=component_to_dict(obj=self.chat_generator, name="chat_generator"),
                               max_concurrency=self.max_concurrency, max_retries=self.max_retries,
                               pricing=self.pricing, max_words=self.max_words)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliverableSummarizer":
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

    def _summarize_one(self, text: DeliverableText) -> DeliverableRecord:
        pub = text.publication
        body, truncated = documents_text(text, self.max_words)
        messages = [ChatMessage.from_system(SUMMARY_SYSTEM),
                    ChatMessage.from_user(SUMMARY_TEMPLATE.format(title=pub.title, url=pub.url, text=body))]
        error = "no attempt"
        for attempt in range(1, self.max_retries + 2):
            t0 = time.monotonic()
            reply = self.chat_generator.run(messages=messages)["replies"][0]
            meta = reply.meta or {}
            usage = meta.get("usage") or {}
            try:
                content = parse_deliverable_content(reply.text or "")
                result = "ok"
            except ValueError as exc:
                content, result, error = None, "invalid_json", str(exc)
            runlog.event("DeliverableSummarizer", "llm_call", pub.publication_id, result,
                         duration_ms=int((time.monotonic() - t0) * 1000), attempt=attempt,
                         model=meta.get("model"), input_tokens=usage.get("prompt_tokens", 0),
                         output_tokens=usage.get("completion_tokens", 0),
                         cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                         cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                         cost_usd=self._cost(meta), truncated=truncated)
            if content is not None:
                return DeliverableRecord(text=text, content=content, model=str(meta.get("model", "unknown")))
            messages = [*messages, reply, ChatMessage.from_user(SUMMARY_RETRY.format(error=error))]
        raise ValueError(error)

    @component.output_types(records=list[DeliverableRecord], failed=list[str], quota_exceeded=bool)
    def run(self, texts: list[DeliverableText]) -> dict[str, Any]:
        self.warm_up()
        stop = threading.Event()
        results: dict[int, DeliverableRecord] = {}
        failed: list[str] = []

        def work(i: int, text: DeliverableText) -> None:
            pid = text.publication.publication_id
            if stop.is_set():
                runlog.event("DeliverableSummarizer", "error", pid, "skipped_quota")
                failed.append(pid)
                return
            try:
                results[i] = self._summarize_one(text)
            except Exception as exc:
                quota = isinstance(exc, LLMQuotaExceeded) or "RateLimit" in type(exc).__name__
                if quota:
                    stop.set()
                runlog.event("DeliverableSummarizer", "error", pid, "quota_exceeded" if quota else "summary_failed",
                             error=str(exc)[:300])
                failed.append(pid)

        with ThreadPoolExecutor(max_workers=max(1, self.max_concurrency)) as pool:
            for i, text in enumerate(texts):
                pool.submit(contextvars.copy_context().run, work, i, text)
        return {"records": [results[i] for i in sorted(results)], "failed": failed,
                "quota_exceeded": stop.is_set()}


@component
class DeliverableWriter:
    """Met a jour output/state/deliverables.json et la section Deliverables des fichiers projet concernes."""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        self._store: ProjectStore | None = None

    @component.output_types(stats=dict[str, int])
    def run(self, records: list[DeliverableRecord], unchanged: list[Publication] | None = None,
            discovered: list[Publication] | None = None, complete: bool = False) -> dict[str, Any]:
        state = DeliverableStore(self.output_dir)
        today = date.today().isoformat()
        affected: set[str] = set()
        for rec in records:
            pub = rec.text.publication
            old = state.get(pub.publication_id)
            affected |= set(pub.project_ids) | set(old.project_ids if old else [])
            state.put(DeliverableEntry(
                publication_id=pub.publication_id, url=pub.url, title=pub.title, date=pub.date,
                project_ids=list(pub.project_ids), files_key=pub.files_key or "",
                files=[{"document": f.file.document, "url": f.file.url, "language": f.file.language,
                        "filename": f.file.filename, "pages": f.pages, "words": f.words} for f in rec.text.files],
                full_text_path=rec.text.full_text_path, content=rec.content, model=rec.model, processed=today,
                sitemap_lastmod=pub.sitemap_lastmod or (old.sitemap_lastmod if old else None)))
            runlog.event("DeliverableWriter", "write", pub.publication_id, "publication", projects=pub.project_ids)
        for pub in unchanged or []:
            old = state.get(pub.publication_id)
            if old is not None and sorted(old.project_ids) != sorted(pub.project_ids):
                affected |= set(old.project_ids) | set(pub.project_ids)
                old = old.model_copy(update={"project_ids": list(pub.project_ids)})
                state.put(old)
            # Page relue et inchangee : sa date du sitemap devient la reference (pas de date : on garde la connue).
            if old is not None and pub.sitemap_lastmod and old.sitemap_lastmod != pub.sitemap_lastmod:
                state.put(old.model_copy(update={"sitemap_lastmod": pub.sitemap_lastmod}))
        orphans_removed = 0
        if complete and discovered is not None:
            discovered_ids = {p.publication_id for p in discovered}
            for pid, old in state.all().items():
                if pid not in discovered_ids:
                    affected |= set(old.project_ids)
                    state.remove(pid)
                    orphans_removed += 1
                    runlog.event("DeliverableWriter", "write", pid, "orphan_removed")
        state.save()
        store = self._store if self._store is not None else shared_store(self.output_dir)
        updated = 0
        for project_id in sorted(affected):
            text = store.read_project_file(project_id)
            if text is None:
                continue
            fm, body = parse_front_matter(text)
            entries = state.for_project(project_id)
            new = render(with_deliverables_count(fm, len(entries)),
                         replace_deliverables_section(body, deliverables_section(entries)))
            if new != text:
                store.write_project_file(project_id, new)
                updated += 1
                runlog.event("DeliverableWriter", "write", project_id, "project_section", deliverables=len(entries))
        return {"stats": {"publications": len(records), "projects_updated": updated,
                          "orphans_removed": orphans_removed}}
