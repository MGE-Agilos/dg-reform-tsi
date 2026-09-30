"""Etage livrables : publications liees aux projets, PDF, texte integral, resume IA, section des fichiers projet."""

import io
from pathlib import Path
from typing import Any

import pdfplumber
from haystack import component

from tsi_agent import runlog
from tsi_agent.deliverables_state import DeliverableStore
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import slug_from_url
from tsi_agent.models import DeliverableFileText, DeliverableText, Publication
from tsi_agent.parsers.publication import files_key, parse_publication, publication_links, select_files
from tsi_agent.render import render
from tsi_agent.store import ProjectStore, _retry_on_lock, file_stem, shared_store


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
    """Liens de publication des pages detail, lues dans le cache HTTP (le run d'extraction vient de le rafraichir)."""

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher = None
        self._store: ProjectStore | None = None

    @component.output_types(publications=list[Publication])
    def run(self) -> dict[str, Any]:
        store = self._store if self._store is not None else shared_store(self.output_dir)
        fetcher = self._get_fetcher()
        by_url: dict[str, Publication] = {}
        missing: list[str] = []
        projects = 0
        for project_id, entry in sorted(store.all().items()):
            if not entry.detail_page or not entry.listed or not entry.source_urls:
                continue
            html = fetcher.cached(entry.source_urls[0])  # source_urls[0] = page detail (render.source_urls)
            if html is None:
                missing.append(project_id)
                continue
            links = publication_links(html)
            projects += bool(links)
            for url in links:
                pub = by_url.setdefault(url, Publication(publication_id=slug_from_url(url), url=url))
                if project_id not in pub.project_ids:
                    pub.project_ids.append(project_id)
        if missing:
            runlog.event("PublicationDiscoverer", "alert", None, "warning", missing=missing[:20],
                         message=f"{len(missing)} detail page(s) not in the HTTP cache: run `tsi_agent run` first")
        publications = list(by_url.values())
        runlog.event("PublicationDiscoverer", "discover", None, publications=len(publications),
                     projects_with_publications=projects, missing_pages=len(missing))
        return {"publications": publications}


@component
class PublicationFetcher(_Http):
    """Pages de publication ; seules les publications nouvelles ou dont les fichiers ont change sont retraitees."""

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher = None
        self._offered: set[str] = set()  # deja proposees pendant ce run (boucle par lots du runner)

    @component.output_types(to_process=list[Publication], unchanged=list[Publication], failed=list[str],
                            remaining=int)
    def run(self, publications: list[Publication], full: bool = False, limit: int | None = None) -> dict[str, Any]:
        known = DeliverableStore(self.output_dir)
        to_process: list[Publication] = []
        unchanged: list[Publication] = []
        failed: list[str] = []
        remaining = 0
        for pub in publications:
            if pub.url in self._offered:
                continue
            if limit is not None and len(to_process) >= limit:
                remaining += 1
                continue
            self._offered.add(pub.url)
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
            entry = known.get(pub.publication_id)
            if entry is not None and entry.files_key == pub.files_key and not full:
                unchanged.append(pub)
                continue
            runlog.event("PublicationFetcher", "classify", pub.publication_id, "changed" if entry else "new",
                         files=len(files))
            to_process.append(pub)
        return {"to_process": to_process, "unchanged": unchanged, "failed": failed, "remaining": remaining}


def pdf_text(data: bytes) -> tuple[int, str]:
    """(nombre de pages, texte) ; pages separees par un marqueur."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [f"--- page {i} ---\n{page.extract_text() or ''}" for i, page in enumerate(pdf.pages, start=1)]
    return len(pages), "\n\n".join(pages)


@component
class DeliverableTextExtractor(_Http):
    """Telecharge les PDF retenus (sans cache HTTP), extrait le texte, ecrit output/deliverables/<id>.md."""

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
        if not data.startswith(b"%PDF"):
            self._alert(pub, url, "not a PDF")
            return None
        try:
            pages, text = pdf_text(data)
        except Exception as exc:  # pdfplumber/pdfminer levent des types varies sur un PDF abime
            self._alert(pub, url, f"unreadable PDF: {type(exc).__name__}")
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
