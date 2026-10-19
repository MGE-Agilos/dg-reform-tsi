"""Progression par lots : chaque lot lit au plus N pages et est enregistre avant le suivant (interruption sans perte)."""

import math

import pytest

from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from test_deliverables import pipeline_settings
from tsi_agent.components.deliverables import PublicationFetcher
from tsi_agent.config import Settings
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
from tsi_agent.deliverables_state import DeliverableStore
from tsi_agent.models import Publication
from tsi_agent.runner import run_deliverables
from tsi_agent.store import LocalProjectStore, StoreEntry

SITE = "https://reforms-investments.ec.europa.eu"
SITEMAP = f"{SITE}/sitemap.xml"
N = 23
OLD = "2026-01-01"


def pub_url(i):
    return f"{SITE}/publications-0/pub-{i:02d}_en"


def detail_url(i):
    return f"{SITE}/technical-support-instrument-0/health-and-long-term-care/project-{i:02d}_en"


def urlset(dates):
    body = "".join(f"<url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in dates.items())
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>").encode()


PUB_HTML = fixture_bytes("pub_one_file.html")


def seed_state(tmp_path, n=N):
    """n projets avec une publication deja traitee (fichiers identiques a la page), sans date du sitemap."""
    store = LocalProjectStore(tmp_path)
    ds = DeliverableStore(tmp_path)
    probe = PublicationFetcher(output_dir=str(tmp_path / "probe"))
    probe._fetcher = FakeFetcher(pages={pub_url(0): PUB_HTML})
    key = probe.run(publications=[Publication(publication_id="pub-00", url=pub_url(0))])["to_process"][0].files_key
    from test_deliverables import entry
    for i in range(n):
        pid = f"project-{i:02d}"
        store.put(StoreEntry(project_id=pid, file=f"projects/{pid}.md", detail_page=True,
                             source_urls=[detail_url(i)], publication_urls=[pub_url(i)], first_seen="d",
                             last_updated="d"), save=False)
        ds.put(entry(pid=f"pub-{i:02d}", projects=(pid,)).model_copy(
            update={"url": pub_url(i), "files_key": key, "processed": OLD}))
    store.flush()
    ds.save()
    return store


class CountingSite(FakeFetcher):
    """Pages de publication identiques ; KeyboardInterrupt (SIGINT du budget de temps) a la lecture n° `stop_at`."""

    def __init__(self, dates, stop_at=None):
        super().__init__(pages={SITEMAP: urlset(dates), **{pub_url(i): PUB_HTML for i in range(N)}})
        self.stop_at = stop_at
        self.pub_calls: list[str] = []

    def get(self, url, cache=True):
        if "/publications-0/" in url:
            self.pub_calls.append(url)
            if self.stop_at is not None and len(self.pub_calls) == self.stop_at:
                raise KeyboardInterrupt
        return super().get(url, cache)


def factory(tmp_path, site, store):
    def make(settings, **kw):
        p = build_deliverables_pipeline(settings, chat_generator=FakeChatGenerator(), sitemap_url=kw.get("sitemap_url"))
        for name in ("discoverer", "publications", "texts"):
            p.get_component(name)._fetcher = site
        p.get_component("discoverer")._store = store
        p.get_component("writer")._store = store
        return p
    return make


def settings_for(tmp_path, fetch_chunk=5):
    return pipeline_settings(tmp_path).model_copy(update={"chunk_size": 25, "deliverables_fetch_chunk": fetch_chunk})


DATES = {pub_url(i): "2025-12-01T15:25Z" for i in range(N)}


def dated(tmp_path):
    return sorted(pid for pid, e in DeliverableStore(tmp_path).all().items() if e.sitemap_lastmod)


def test_setting_default():
    assert Settings().deliverables_fetch_chunk == 25


def test_unchanged_only_run_fetches_by_chunks_each_page_once_and_records_every_date(tmp_path):
    store = seed_state(tmp_path)
    site = CountingSite(DATES)
    summary = run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d1",
                               pipeline_factory=factory(tmp_path, site, store))
    assert summary["chunks"] == math.ceil(N / 5)
    assert sorted(site.pub_calls) == sorted(set(site.pub_calls)) and len(site.pub_calls) == N  # chacune une fois
    assert len(dated(tmp_path)) == N and summary["written"]["publications"] == 0
    assert summary["remaining_publications"] == 0


@pytest.mark.parametrize("k", [1, 3])
def test_interruption_after_chunk_k_keeps_the_dates_of_the_completed_chunks(tmp_path, k):
    store = seed_state(tmp_path)
    site = CountingSite(DATES, stop_at=5 * k + 1)  # premiere lecture du lot k+1
    with pytest.raises(KeyboardInterrupt):
        run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d1",
                         pipeline_factory=factory(tmp_path, site, store))
    assert dated(tmp_path) == [f"pub-{i:02d}" for i in range(5 * k)]
    # Run suivant : seules les pages restantes sont lues.
    site2 = CountingSite(DATES)
    run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d2",
                     pipeline_factory=factory(tmp_path, site2, store))
    assert sorted(site2.pub_calls) == [pub_url(i) for i in range(5 * k, N)]
    assert len(dated(tmp_path)) == N


def test_recheck_all_progresses_across_chunks_without_rereading(tmp_path):
    store = seed_state(tmp_path)
    run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d1",
                     pipeline_factory=factory(tmp_path, CountingSite(DATES), store))
    ds = DeliverableStore(tmp_path)
    for pid, e in ds.all().items():  # au-dela de recheck_days
        ds.put(e.model_copy(update={"processed": OLD}))
    ds.save()
    site = CountingSite(DATES)
    summary = run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d2", recheck_all=True,
                               pipeline_factory=factory(tmp_path, site, store))
    assert len(site.pub_calls) == N and len(set(site.pub_calls)) == N
    assert summary["chunks"] == math.ceil(N / 5) and summary["skipped_by_sitemap"] == 0


def test_fetch_limit_counts_fetches_not_llm_sends(tmp_path):
    seed_state(tmp_path, n=4)
    f = PublicationFetcher(output_dir=str(tmp_path))
    f._fetcher = FakeFetcher(pages={pub_url(i): PUB_HTML for i in range(4)})
    pubs = [Publication(publication_id=f"pub-{i:02d}", url=pub_url(i), project_ids=[f"project-{i:02d}"])
            for i in range(4)]
    first = f.run(publications=pubs, fetch_limit=3)
    assert first["fetched"] == 3 and first["remaining"] == 1 and len(first["unchanged"]) == 3
    second = f.run(publications=pubs, fetch_limit=3)
    assert second["fetched"] == 1 and second["remaining"] == 0
    assert f.run(publications=pubs, fetch_limit=3)["fetched"] == 0


# --- etage run : meme garantie --------------------------------------------------------------------------------

def test_run_interrupted_mid_run_keeps_the_dates_of_completed_chunks(tmp_path):
    from test_incremental_sitemap import SOURCES, Site, detail_urls, factory as run_factory
    from tsi_agent.runner import run_extraction
    from tsi_agent.urls import is_canonical_detail
    s = Settings(output_dir=str(tmp_path / "output"), logs_dir=str(tmp_path / "logs"),
                 cache_dir=str(tmp_path / "cache"), min_interval_s=0, chunk_size=20)
    run_extraction(s, SOURCES, countries=["HR"], run_id="init",
                   pipeline_factory=run_factory(FakeChatGenerator(), Site({})))
    urls = detail_urls(s)
    dates = {u: "2025-12-01T15:25Z" for u in urls}

    class Interrupting(Site):
        def __init__(self):
            super().__init__(dates)
            self.detail_reads = 0

        def get(self, url, cache=True):
            if url in dates and is_canonical_detail(url):
                self.detail_reads += 1
                if self.detail_reads == 31:  # au milieu du 2e lot (20 pages par lot)
                    raise KeyboardInterrupt
            return super().get(url, cache)

    with pytest.raises(KeyboardInterrupt):
        run_extraction(s, SOURCES, run_id="cut", pipeline_factory=run_factory(FakeChatGenerator(), Interrupting()))
    recorded = [e for e in LocalProjectStore(s.output_dir).all().values() if e.sitemap_lastmod]
    assert len(recorded) >= 20  # le 1er lot complet est enregistre
    assert len(recorded) < len([u for u in urls if is_canonical_detail(u)])


# --- sans sitemap frais : publications connues et datees non relues ------------------------------------------------

class ThrottledSite(CountingSite):
    """Sitemap toujours en HTTP 429."""

    def __init__(self, dates):
        super().__init__(dates)
        self.sitemap_calls = 0

    def get(self, url, cache=True):
        if url == SITEMAP:
            self.sitemap_calls += 1
            from tsi_agent.http import FetchError
            raise FetchError(url, "HTTP 429", 429)
        return super().get(url, cache)


def dated_state(tmp_path):
    store = seed_state(tmp_path)
    run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d0",
                     pipeline_factory=factory(tmp_path, CountingSite(DATES), store))
    ds = DeliverableStore(tmp_path)
    for pid, e in ds.all().items():  # au-dela de recheck_days
        ds.put(e.model_copy(update={"processed": OLD}))
    ds.save()
    assert len(dated(tmp_path)) == N
    return store


def test_unavailable_sitemap_keeps_known_dated_publications_without_fetching(tmp_path):
    from tsi_agent import __main__ as cli
    store = dated_state(tmp_path)
    before = {pid: e.sitemap_lastmod for pid, e in DeliverableStore(tmp_path).all().items()}
    site = ThrottledSite(DATES)
    summary = run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d1",
                               pipeline_factory=factory(tmp_path, site, store))
    assert site.pub_calls == [] and summary["skipped_no_sitemap"] == N
    assert summary["sitemap_status"] == "stale" and summary["sitemap_read"] is False  # instantane de d0
    assert summary["counts"]["classify:unchanged_no_sitemap"] == N
    assert {pid: e.sitemap_lastmod for pid, e in DeliverableStore(tmp_path).all().items()} == before
    assert cli.run_exit_code(summary) == 3


def test_unavailable_sitemap_still_fetches_undated_publications_and_recheck_all(tmp_path):
    store = dated_state(tmp_path)
    ds = DeliverableStore(tmp_path)
    ds.put(ds.get("pub-00").model_copy(update={"sitemap_lastmod": None}))
    ds.save()
    site = ThrottledSite(DATES)
    summary = run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d1",
                               pipeline_factory=factory(tmp_path, site, store))
    assert site.pub_calls == [pub_url(0)] and summary["skipped_no_sitemap"] == N - 1
    site = ThrottledSite(DATES)
    run_deliverables(settings_for(tmp_path), sitemap_url=SITEMAP, run_id="d2", recheck_all=True,
                     pipeline_factory=factory(tmp_path, site, store))
    assert len(site.pub_calls) == N


@pytest.mark.parametrize("recent, calls", [(True, 1), (False, None)])
def test_deliverables_skips_the_patient_read_right_after_a_failed_run_read(tmp_path, recent, calls):
    import json
    import time
    from tsi_agent.components.listing import STATUS_PATH
    store = dated_state(tmp_path)
    marker = tmp_path / STATUS_PATH
    marker.write_text(json.dumps({"status": "unavailable", "at": time.time() - (60 if recent else 10 * 3600)}),
                      encoding="utf-8")
    s = settings_for(tmp_path)
    site = ThrottledSite(DATES)
    run_deliverables(s, sitemap_url=SITEMAP, run_id="d1", pipeline_factory=factory(tmp_path, site, store))
    # Quelques minutes apres l'echec de `run` : une seule tentative ; sinon lecture patiente (+ nouvelle lecture).
    assert site.sitemap_calls == (calls if calls is not None else 2 * s.sitemap_attempts)
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "stale"
