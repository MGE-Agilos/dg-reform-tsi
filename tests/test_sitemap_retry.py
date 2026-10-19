"""Sitemap en HTTP 429 : une nouvelle lecture apres une attente, puis la copie en cache d'un sitemap enfant."""

import pytest

import tsi_agent.components.listing as listing_mod
from conftest import FakeFetcher
from tsi_agent import runlog
from tsi_agent.components.listing import SourceListingFetcher
from tsi_agent.config import Settings
from tsi_agent.http import FetchError

INDEX = "https://reforms-investments.ec.europa.eu/sitemap.xml"
CHILD1 = f"{INDEX}_en?page=1"
CHILD2 = f"{INDEX}_en?page=2"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
INDEX_XML = (f'<?xml version="1.0"?><sitemapindex {NS}><sitemap><loc>{CHILD1}</loc></sitemap>'
             f'<sitemap><loc>{CHILD2}</loc></sitemap></sitemapindex>').encode()


def child(n):
    return (f'<?xml version="1.0"?><urlset {NS}><url><loc>https://x/p{n}_en</loc><lastmod>2025-01-01</lastmod>'
            f'</url></urlset>').encode()


class Flaky(FakeFetcher):
    """`fails[url]` premieres lectures en HTTP 429."""

    def __init__(self, fails, cached_pages=None):
        super().__init__(pages={INDEX: INDEX_XML, CHILD1: child(1), CHILD2: child(2)}, cached_pages=cached_pages)
        self.fails = dict(fails)

    def get(self, url, cache=True):
        if self.fails.get(url, 0) > 0:
            self.fails[url] -= 1
            self.calls.append(url)
            raise FetchError(url, "HTTP 429", 429)
        return super().get(url, cache)


@pytest.fixture
def waits(monkeypatch):
    calls = []
    monkeypatch.setattr(listing_mod, "_sleep", calls.append)
    return calls


def listing(fetcher, wait=0):
    lf = SourceListingFetcher(sources={"sitemap_url": INDEX}, sitemap_retry_wait_s=wait)
    lf._fetcher = fetcher
    return lf


def test_default_wait_is_configurable_and_is_180_s():
    assert Settings().sitemap_retry_wait_s == 180
    assert SourceListingFetcher(sources={}).sitemap_retry_wait_s == 180


def test_incomplete_sitemap_is_read_again_after_the_wait(tmp_path, waits):
    with runlog.open_run(tmp_path, "r") as run:
        pages = listing(Flaky({CHILD1: 1}), wait=7).fetch_sitemap()
    assert [p.url for p in pages] == [CHILD1, CHILD2] and waits == [7]
    assert run.counts["error:sitemap_incomplete"] == 1


def test_child_still_failing_falls_back_to_its_cached_copy(tmp_path, waits):
    with runlog.open_run(tmp_path, "r") as run:
        pages = listing(Flaky({CHILD2: 5}, cached_pages={CHILD2: child(9)})).fetch_sitemap()
    assert [p.url for p in pages] == [CHILD1, CHILD2] and pages[1].content == child(9)
    assert run.counts["sitemap:sitemap_child_cached"] == 1 and waits == [0]


def test_child_without_cached_copy_leaves_the_sitemap_unread(tmp_path, waits):
    assert listing(Flaky({CHILD2: 5})).fetch_sitemap() == []


def test_index_is_never_taken_from_the_cache(tmp_path, waits):
    assert listing(Flaky({INDEX: 5}, cached_pages={INDEX: INDEX_XML})).fetch_sitemap() == []


def test_the_wait_happens_once_per_run(tmp_path, waits):
    lf = listing(Flaky({CHILD2: 10}), wait=3)
    assert lf.fetch_sitemap() == [] and lf.fetch_sitemap() == []
    assert waits == [3]


def test_pipeline_passes_the_configured_wait(tmp_path):
    from tsi_agent.pipeline import build_pipeline
    from conftest import FakeChatGenerator
    s = Settings(output_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c"), sitemap_retry_wait_s=42)
    pipe = build_pipeline(s, {"sitemap_url": INDEX}, dry_run=True, judge_generator=FakeChatGenerator())
    assert pipe.get_component("listing").sitemap_retry_wait_s == 42
