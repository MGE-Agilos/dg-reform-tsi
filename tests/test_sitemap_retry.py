"""Sitemap en HTTP 429 : requetes espacees, tentatives patientes, nouvelle lecture, puis copies anciennes.

Les copies anciennes (cache HTTP d'un enfant, instantane de l'etat) servent a la decouverte seulement : pages
"sitemap_stale", jamais rendues par fetch_sitemap (dont les appelants se servent pour ne pas relire des pages).
"""

import json

import pytest

import tsi_agent.components.listing as listing_mod
from conftest import FakeFetcher
from tsi_agent import runlog
from tsi_agent.components.listing import SNAPSHOT_PATH, SourceListingFetcher
from tsi_agent.config import Settings
from tsi_agent.http import FetchError
from tsi_agent.parsers.sitemap import parse_sitemap, sitemap_lastmods

INDEX = "https://reforms-investments.ec.europa.eu/sitemap.xml"
CHILD1 = f"{INDEX}_en?page=1"
CHILD2 = f"{INDEX}_en?page=2"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
INDEX_XML = (f'<?xml version="1.0"?><sitemapindex {NS}><sitemap><loc>{CHILD1}</loc></sitemap>'
             f'<sitemap><loc>{CHILD2}</loc></sitemap></sitemapindex>').encode()


def child(n, lastmod="2025-01-01"):
    return (f'<?xml version="1.0"?><urlset {NS}><url><loc>https://x/p{n}_en</loc><lastmod>{lastmod}</lastmod>'
            f'</url></urlset>').encode()


class Flaky(FakeFetcher):
    """`fails[url]` premieres lectures en HTTP 429 (Retry-After `retry_after`)."""

    def __init__(self, fails, cached_pages=None, retry_after=None):
        super().__init__(pages={INDEX: INDEX_XML, CHILD1: child(1), CHILD2: child(2)}, cached_pages=cached_pages)
        self.fails = dict(fails)
        self.retry_after = retry_after

    def get(self, url, cache=True):
        if self.fails.get(url, 0) > 0:
            self.fails[url] -= 1
            self.calls.append(url)
            raise FetchError(url, "HTTP 429", 429, retry_after=self.retry_after)
        return super().get(url, cache)


@pytest.fixture
def waits(monkeypatch):
    calls = []
    monkeypatch.setattr(listing_mod, "_sleep", calls.append)
    return calls


def listing(fetcher, output_dir=None, **kw):
    options = {"sitemap_retry_wait_s": 0, "sitemap_interval_s": 0, "sitemap_attempts": 1, **kw}
    lf = SourceListingFetcher(sources={"sitemap_url": INDEX}, output_dir=output_dir, **options)
    lf._fetcher = fetcher
    return lf


def test_defaults_are_configurable():
    s = Settings()
    assert (s.sitemap_retry_wait_s, s.sitemap_interval_s, s.sitemap_attempts, s.sitemap_backoff_s,
            s.sitemap_budget_s) == (180, 10, 4, [60, 120, 240], 1200)
    lf = SourceListingFetcher(sources={})
    assert (lf.sitemap_retry_wait_s, lf.sitemap_interval_s, lf.sitemap_attempts, lf.sitemap_backoff_s) == \
        (180, 10, 4, [60, 120, 240])


def test_incomplete_sitemap_is_read_again_after_the_wait(tmp_path, waits):
    with runlog.open_run(tmp_path, "r") as run:
        pages = listing(Flaky({CHILD1: 1}), sitemap_retry_wait_s=7).fetch_sitemap()
    assert [p.url for p in pages] == [CHILD1, CHILD2] and waits == [7]
    assert run.counts["error:sitemap_incomplete"] == 1


def test_sitemap_requests_are_paced_and_429_retried_patiently_honouring_retry_after(tmp_path, waits):
    lf = listing(Flaky({CHILD1: 2}, retry_after=90), sitemap_interval_s=10, sitemap_attempts=4,
                 sitemap_backoff_s=[60, 120, 240])
    with runlog.open_run(tmp_path, "r") as run:
        read = lf.read_sitemap()
    assert read.status == "fresh"
    # index ; enfant 1 : 429 (attente max(60, Retry-After 90)), 429 (120), ok ; enfant 2 ; 10 s avant chaque requete
    assert waits == [10, 90, 10, 120, 10, 10]
    assert run.counts["sitemap:rate_limited"] == 2


def test_patience_stops_at_the_budget(tmp_path, monkeypatch):
    clock, waits = [0.0], []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(listing_mod, "_sleep", sleep)
    monkeypatch.setattr(listing_mod, "_monotonic", lambda: clock[0])
    lf = listing(Flaky({CHILD1: 99}), sitemap_attempts=4, sitemap_backoff_s=[60], sitemap_budget_s=100)
    assert lf.read_sitemap().status == "unavailable"
    assert waits.count(60) == 1  # une attente de 60 s tient dans les 100 s du budget, pas deux


def test_child_still_failing_falls_back_to_its_cached_copy_for_discovery_only(tmp_path, waits):
    lf = listing(Flaky({CHILD2: 5}, cached_pages={CHILD2: child(9)}))
    with runlog.open_run(tmp_path, "r") as run:
        read = lf.read_sitemap()
    assert read.status == "stale"
    assert [(p.url, p.kind) for p in read.pages] == [(CHILD1, "sitemap"), (CHILD2, "sitemap_stale")]
    assert read.pages[1].content == child(9)
    assert run.counts["sitemap:sitemap_child_cached"] == 1
    assert lf.fetch_sitemap() == []  # jamais pour ne pas relire une page


def test_child_without_cached_copy_or_snapshot_leaves_the_sitemap_unread(tmp_path, waits):
    read = listing(Flaky({CHILD2: 5})).read_sitemap()
    assert read.status == "unavailable" and [p.url for p in read.pages] == [CHILD1]  # lu maintenant : garde


def test_index_is_never_taken_from_the_cache(tmp_path, waits):
    read = listing(Flaky({INDEX: 5}, cached_pages={INDEX: INDEX_XML})).read_sitemap()
    assert read.status == "unavailable" and read.pages == []


def test_sitemap_is_read_once_per_instance_failure_included(tmp_path, waits):
    site = Flaky({CHILD2: 10})
    lf = listing(site, sitemap_retry_wait_s=3)
    assert lf.fetch_sitemap() == [] and lf.fetch_sitemap() == [] and lf.read_sitemap().status == "unavailable"
    assert waits == [3] and site.calls.count(CHILD2) == 2  # lecture + nouvelle lecture, pas plus


def test_full_read_persists_a_snapshot(tmp_path, waits):
    listing(Flaky({}), output_dir=str(tmp_path)).read_sitemap()
    data = json.loads((tmp_path / SNAPSHOT_PATH).read_text(encoding="utf-8"))
    assert data["index_url"] == INDEX and data["fetched_at"]
    assert data["urls"] == {"https://x/p1_en": "2025-01-01", "https://x/p2_en": "2025-01-01"}


def test_snapshot_is_used_for_discovery_when_the_fresh_read_fails(tmp_path, waits):
    listing(Flaky({}), output_dir=str(tmp_path)).read_sitemap()
    with runlog.open_run(tmp_path / "logs", "r") as run:
        lf = listing(Flaky({INDEX: 9}), output_dir=str(tmp_path))
        read = lf.read_sitemap()
    assert read.status == "stale" and [p.kind for p in read.pages] == ["sitemap_stale"]
    assert parse_sitemap(read.pages[0].content)[1] == ["https://x/p1_en", "https://x/p2_en"]
    assert sitemap_lastmods(read.pages[0].content)["https://x/p1_en"] == "2025-01-01"
    assert run.counts["sitemap:snapshot_used"] == 1 and lf.fetch_sitemap() == []
    # Une lecture incomplete ne remplace pas l'instantane.
    assert json.loads((tmp_path / SNAPSHOT_PATH).read_text(encoding="utf-8"))["urls"]


def test_pipeline_passes_the_configured_sitemap_options(tmp_path):
    from conftest import FakeChatGenerator
    from tsi_agent.pipeline import build_pipeline
    s = Settings(output_dir=str(tmp_path / "o"), cache_dir=str(tmp_path / "c"), sitemap_retry_wait_s=42,
                 sitemap_attempts=2, sitemap_backoff_s=[5])
    pipe = build_pipeline(s, {"sitemap_url": INDEX}, dry_run=True, judge_generator=FakeChatGenerator())
    lf = pipe.get_component("listing")
    assert (lf.sitemap_retry_wait_s, lf.sitemap_attempts, lf.sitemap_backoff_s, lf.output_dir) == \
        (42, 2, [5], s.output_dir)
