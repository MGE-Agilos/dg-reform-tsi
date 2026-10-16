from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, slug_from_iso
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.models import RawPage
from tsi_agent.parsers.sitemap import parse_sitemap

NAME = "SourceListingFetcher"


@component
class SourceListingFetcher:
    """Récupère toutes les pages de listing et les documents programme décrits dans `sources`."""

    def __init__(self, sources: dict[str, Any], cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.sources = sources
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

    def _sitemap_pages(self, index_url: str) -> list[RawPage]:
        """Pages du sitemap (une RawPage par sitemap enfant, key = URL de l'index) ; [] si l'une manque.

        Tout ou rien : un sitemap partiel ferait passer des projets pour absents du site.
        """
        try:
            index = self._get_fetcher().get(index_url)
            children, urls = parse_sitemap(index.content)
            if not children:  # sitemap simple, sans index
                return [RawPage(kind="sitemap", key=index_url, url=index_url, content=index.content)]
            pages = []
            for child in children:
                result = self._get_fetcher().get(child)
                parse_sitemap(result.content)
                pages.append(RawPage(kind="sitemap", key=index_url, url=child, content=result.content))
            return pages
        except (FetchError, OSError, ValueError) as exc:
            runlog.event(NAME, "error", index_url, "sitemap_incomplete", error=repr(exc))
            return []

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
