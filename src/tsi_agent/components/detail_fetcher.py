from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import sha256_text
from tsi_agent.models import DetailPage, ProjectRef
from tsi_agent.parsers.detail import parse_detail
from tsi_agent.store import ProjectStore, shared_store

NAME = "DetailPageFetcher"
GONE_STATUSES = (404, 410)


@component
class DetailPageFetcher:
    """Récupère les pages détail ; seules celles dont le texte a changé partent vers le LLM."""

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher: PoliteFetcher | None = None
        self._store: ProjectStore | None = None

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    def _get_store(self) -> ProjectStore:
        if self._store is None:
            self._store = shared_store(self.output_dir)
        return self._store

    @component.output_types(pages=list[DetailPage], unchanged=list[ProjectRef], failed=list[str])
    def run(self, refs: list[ProjectRef], full: bool = False) -> dict[str, Any]:
        pages, unchanged, failed = [], [], []
        for ref in refs:
            try:
                result = self._get_fetcher().get(ref.detail_url)
                parsed = parse_detail(result.content)
            except FetchError as exc:
                failed.append(ref.project_id)
                runlog.event(NAME, "error", ref.project_id, "fetch_failed", error=str(exc))
                if exc.status in GONE_STATUSES:
                    runlog.event(NAME, "alert", ref.project_id, "warning",
                                 message=f"Detail page gone ({exc.status}): {ref.detail_url}")
                continue
            except OSError as exc:
                failed.append(ref.project_id)
                runlog.event(NAME, "error", ref.project_id, "fetch_failed", error=repr(exc))
                continue
            content_hash = sha256_text(parsed.text)
            entry = self._get_store().get(ref.project_id)
            if not full and entry is not None and entry.content_hash == content_hash:
                unchanged.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "unchanged")
                continue
            pages.append(DetailPage(ref=ref, url=result.final_url, title=parsed.title or ref.title,
                                    text=parsed.text, content_hash=content_hash,
                                    programme=parsed.programme, year=parsed.year, intro=parsed.intro,
                                    sections=parsed.sections, results=parsed.results))
            runlog.event(NAME, "classify", ref.project_id, "changed" if entry else "new")
        return {"pages": pages, "unchanged": unchanged, "failed": failed}
