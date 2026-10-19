import time
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, slug_from_iso
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.models import RawPage
from tsi_agent.parsers.sitemap import parse_sitemap

NAME = "SourceListingFetcher"
_sleep = time.sleep  # remplace par les tests


@component
class SourceListingFetcher:
    """Récupère toutes les pages de listing et les documents programme décrits dans `sources`."""

    def __init__(self, sources: dict[str, Any], cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT, sitemap_retry_wait_s: float = 180):
        """:param sitemap_retry_wait_s: attente avant la nouvelle lecture d'un sitemap incomplet (HTTP 429)."""
        self.sources = sources
        self.sitemap_retry_wait_s = sitemap_retry_wait_s
        self._waited = False  # une seule attente par instance (par run) : les lots suivants ne la repetent pas
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher: PoliteFetcher | None = None

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    def _targets(self, countries: list[str] | None) -> list[tuple[str, str, str, str | None]]:
        s = self.sources
        isos = countries or list(COUNTRIES)
        targets = [("factsheet", iso, s["factsheet_url"].format(slug=slug_from_iso(iso)), None) for iso in isos]
        targets.append(("spotlight", "spotlight", s["spotlight_url"], None))
        targets += [("policy_area", slug, s["policy_area_url"].format(slug=slug), None) for slug in s["policy_areas"]]
        if not countries:  # le PDF SRSP couvre tous les pays : exclu des runs pilotes
            targets.append(("srsp_pdf", "srsp", s["srsp_pdf_url"], None))
        targets += [("programme_doc", d["id"], d["url"], d.get("title")) for d in s.get("programme_docs", [])]
        return targets

    def _read_sitemap(self, index_url: str) -> tuple[list[str], dict[str, RawPage], list[str]] | None:
        """(sitemaps enfants dans l'ordre de l'index, pages lues par URL, enfants en echec) ; None si l'index lui-meme
        est illisible."""
        try:
            index = self._get_fetcher().get(index_url)
            children, _ = parse_sitemap(index.content)
        except (FetchError, OSError, ValueError) as exc:
            runlog.event(NAME, "error", index_url, "sitemap_incomplete", error=repr(exc))
            return None
        if not children:  # sitemap simple, sans index
            return [index_url], {index_url: RawPage(kind="sitemap", key=index_url, url=index_url,
                                                    content=index.content)}, []
        pages: dict[str, RawPage] = {}
        failed: list[str] = []
        for child in children:
            try:
                result = self._get_fetcher().get(child)
                parse_sitemap(result.content)
                pages[child] = RawPage(kind="sitemap", key=index_url, url=child, content=result.content)
            except (FetchError, OSError, ValueError) as exc:
                failed.append(child)
                runlog.event(NAME, "error", index_url, "sitemap_incomplete", child=child, error=repr(exc))
        return children, pages, failed

    def _cached_child(self, index_url: str, child: str) -> RawPage | None:
        """Copie en cache HTTP d'un sitemap enfant illisible ; None si absente ou invalide."""
        try:
            content = self._get_fetcher().cached(child)
            if content is None:
                return None
            parse_sitemap(content)
        except (OSError, ValueError):
            return None
        runlog.event(NAME, "sitemap", child, "sitemap_child_cached")
        return RawPage(kind="sitemap", key=index_url, url=child, content=content)

    def _sitemap_pages(self, index_url: str) -> list[RawPage]:
        """Pages du sitemap (une RawPage par sitemap enfant, key = URL de l'index) ; [] s'il reste incomplet.

        Tout ou rien : un sitemap partiel ferait passer des projets pour absents du site. Lecture incomplete (HTTP
        429) : nouvelle lecture complete apres `sitemap_retry_wait_s` (une attente par run au plus), puis, pour un
        enfant toujours illisible, sa copie du cache HTTP. Un enfant ancien ne fait que manquer des pages nouvelles :
        il ne fait jamais paraitre absente une page qui existe. L'index n'est jamais pris dans le cache (un index
        ancien pourrait omettre un enfant, donc toutes ses pages).
        """
        read = self._read_sitemap(index_url)
        if (read is None or read[2]) and not self._waited:
            self._waited = True
            _sleep(self.sitemap_retry_wait_s)
            read = self._read_sitemap(index_url)
        if read is None:
            return []
        order, pages, failed = read
        for child in failed:
            cached = self._cached_child(index_url, child)
            if cached is None:
                return []
            pages[child] = cached
        return [pages[u] for u in order]

    def fetch_sitemap(self) -> list[RawPage]:
        """Pages du sitemap configure (`sitemap_url`) ; [] si absent, indisponible ou incomplet."""
        url = self.sources.get("sitemap_url")
        return self._sitemap_pages(url) if url else []

    @component.output_types(pages=list[RawPage])
    def run(self, countries: list[str] | None = None) -> dict[str, list[RawPage]]:
        pages = []
        for kind, key, url, title in self._targets(countries):
            try:
                result = self._get_fetcher().get(url)
            except FetchError as exc:
                runlog.event(NAME, "error", url, "fetch_failed", kind=kind, key=key, error=str(exc))
                continue
            except OSError as exc:
                runlog.event(NAME, "error", url, "fetch_failed", kind=kind, key=key, error=repr(exc))
                continue
            pages.append(RawPage(kind=kind, key=key, url=url, content=result.content, title=title))
        if not countries and self.sources.get("sitemap_url"):  # tous pays : exclu des runs pilotes, comme le PDF
            pages += self._sitemap_pages(self.sources["sitemap_url"])
        runlog.event(NAME, "discover", None, "ok", pages=len(pages))
        return {"pages": pages}
