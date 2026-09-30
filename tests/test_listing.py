from tsi_agent import runlog
from tsi_agent.components.listing import SourceListingFetcher
from tsi_agent.http import FetchError, FetchResult

SOURCES = {
    "factsheet_url": "https://x/{slug}_en",
    "spotlight_url": "https://x/spotlight_en",
    "policy_areas": ["green-transition"],
    "policy_area_url": "https://x/{slug}_en",
}


class OneUrlBoom:
    """Renvoie une page vide pour toute URL, sauf `boom_url` qui casse comme indiqué."""

    def __init__(self, boom_url: str, error: Exception):
        self.boom_url = boom_url
        self.error = error
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        if url == self.boom_url:
            raise self.error
        return FetchResult(url, url, 200, b"<html></html>", False)


def test_fetch_error_on_one_page_does_not_stop_the_run():
    fetcher = OneUrlBoom("https://x/spotlight_en", FetchError("https://x/spotlight_en", "HTTP 404", 404))
    f = SourceListingFetcher(sources=SOURCES)
    f._fetcher = fetcher
    out = f.run(countries=["HR"])
    urls = {p.url for p in out["pages"]}
    assert "https://x/spotlight_en" not in urls
    assert "https://x/croatia_en" in urls
    assert "https://x/green-transition_en" in urls


def test_os_error_on_one_page_does_not_stop_the_run(tmp_path):
    fetcher = OneUrlBoom("https://x/spotlight_en", OSError("disk full"))
    f = SourceListingFetcher(sources=SOURCES)
    f._fetcher = fetcher
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        out = f.run(countries=["HR"])
    urls = {p.url for p in out["pages"]}
    assert "https://x/spotlight_en" not in urls
    assert "https://x/croatia_en" in urls
    assert "https://x/green-transition_en" in urls
    assert run.counts["error:fetch_failed"] == 1
