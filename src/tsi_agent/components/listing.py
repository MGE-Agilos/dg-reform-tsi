from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, slug_from_iso
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.models import RawPage

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
        runlog.event(NAME, "discover", None, "ok", pages=len(pages))
        return {"pages": pages}
