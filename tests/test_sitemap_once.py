"""Sitemap lu une fois par run ; sans sitemap frais, pas de relecture de tout le corpus ; copies anciennes pour la
decouverte seulement (run CI du 2026-10-19 : sitemap en HTTP 429, 50 pages detail relues en 60 min)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from tsi_agent import __main__ as cli
from tsi_agent import runlog
from tsi_agent.components.change_detector import ChangeDetector
from tsi_agent.components.discoverer import ProjectDiscoverer
from tsi_agent.components.listing import SNAPSHOT_PATH, urlset_xml
from tsi_agent.config import Settings
from tsi_agent.http import FetchError
from tsi_agent.models import ProjectRef, RawPage
from tsi_agent.runner import run_extraction, seed_sitemap_lastmod
from tsi_agent.store import LocalProjectStore, StoreEntry, shared_store
from tsi_agent.urls import is_canonical_detail
from test_incremental_sitemap import HR, SOURCES, Site, detail_urls, factory, settings  # noqa: F401 (fixture)

SITEMAP = SOURCES["sitemap_url"]
BASE = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"
ONLY_IN_SITEMAP = f"{BASE}/health-and-long-term-care/hems-sitemap-only_en"  # dans aucun listing
DATE = "2025-12-01T15:25Z"


class Throttled(Site):
    """Le sitemap repond toujours HTTP 429."""

    def get(self, url, cache=True):
        if url == SITEMAP:
            self.calls.append(url)
            raise FetchError(url, "HTTP 429", 429)
        return super().get(url, cache)


@pytest.fixture(scope="module")
def corpus_template(tmp_path_factory):
    """Etat d'un mois precedent, construit une fois : run complet avec sitemap frais (dates, instantane)."""
    root = tmp_path_factory.mktemp("corpus")
    s = Settings(output_dir=str(root / "output"), logs_dir=str(root / "logs"), cache_dir=str(root / "cache"),
                 min_interval_s=0, chunk_size=20)
    run_extraction(s, SOURCES, countries=["HR"], run_id="init", pipeline_factory=factory(FakeChatGenerator(), Site({})))
    urls = detail_urls(s)
    dates = {u: DATE for u in urls} | {ONLY_IN_SITEMAP: DATE}
    run_extraction(s, SOURCES, run_id="fresh", recheck_all=True,
                   pipeline_factory=factory(FakeChatGenerator(), Site(dates)))
    known = [u for u in urls if is_canonical_detail(u)] + [ONLY_IN_SITEMAP]
    store = LocalProjectStore(s.output_dir)
    assert all(any(e.sitemap_lastmod for e in store.all().values() if u in e.source_urls) for u in known)
    return Path(s.output_dir), dates, known


@pytest.fixture
def corpus(settings, corpus_template):
    """Copie de l'etat du mois precedent dans le dossier de sortie du test."""
    output, dates, known = corpus_template
    shutil.copytree(output, settings.output_dir)
    return dict(dates), list(known)


def dated(settings) -> int:
    """Projets avec page detail et date du sitemap enregistree (une URL detail peut en porter plusieurs)."""
    return sum(1 for e in LocalProjectStore(settings.output_dir).all().values() if e.detail_page and e.sitemap_lastmod)


# --- une lecture du sitemap par run ------------------------------------------------------------------------------

def test_the_sitemap_is_read_once_per_run_before_the_listings(settings, corpus):
    dates, known = corpus
    site = Site(dates)
    s = settings.model_copy(update={"chunk_size": 5})
    summary = run_extraction(s, SOURCES, run_id="inc", recheck_all=True,
                             pipeline_factory=factory(FakeChatGenerator(), site))
    assert summary["chunks"] > 2
    assert site.calls.count(SITEMAP) == 1
    assert site.calls.index(SITEMAP) < site.calls.index(HR)  # avant les listings


def test_a_failed_sitemap_read_is_not_retried_by_the_next_chunks(settings, corpus):
    dates, known = corpus
    site = Throttled(dates)
    s = settings.model_copy(update={"chunk_size": 1})
    store = shared_store(s.output_dir)  # celui du pipeline ; pages sans date : relues, un lot chacune
    for e in [e for e in store.all().values() if e.detail_page][:3]:
        store.put(e.model_copy(update={"sitemap_lastmod": None}))
    summary = run_extraction(s, SOURCES, run_id="inc", pipeline_factory=factory(FakeChatGenerator(), site))
    assert summary["chunks"] > 2
    # Lecture + une nouvelle lecture (chacune avec ses tentatives sur 429), pour tout le run.
    assert site.calls.count(SITEMAP) == 2 * s.sitemap_attempts


def test_seed_reads_the_sitemap_once(settings, corpus):
    dates, _ = corpus
    site = Site(dates)
    seed_sitemap_lastmod(settings, SOURCES, fetcher=site, run_id="seed")
    assert site.calls.count(SITEMAP) == 1


# --- sans sitemap frais : pas de relecture de masse --------------------------------------------------------------

def test_no_mass_refetch_when_the_sitemap_is_unavailable(settings, corpus):
    dates, known = corpus
    (Path(settings.output_dir) / SNAPSHOT_PATH).unlink()
    site = Throttled(dates)
    with_dates = {pid: e.sitemap_lastmod for pid, e in LocalProjectStore(settings.output_dir).all().items()}
    summary = run_extraction(settings, SOURCES, run_id="no-sitemap",
                             pipeline_factory=factory(FakeChatGenerator(), site))
    assert site.detail_calls(known) == []
    assert summary["sitemap_status"] == "unavailable" and summary["sitemap_read"] is False
    n = dated(settings) - 1  # la page du sitemap seul n'est pas decouverte sans sitemap ni instantane
    assert summary["skipped_no_sitemap"] == n and summary["counts"]["classify:unchanged_no_sitemap"] == n
    assert summary["skipped_by_sitemap"] == 0 and summary["chunks"] == 1
    # Aucune date enregistree ni effacee, rien de dé-liste.
    store = LocalProjectStore(settings.output_dir)
    assert {pid: e.sitemap_lastmod for pid, e in store.all().items()} == with_dates
    assert all(e.listed for e in store.all().values() if e.detail_page)
    assert cli.run_exit_code(summary) == 3


def test_recheck_all_still_rereads_everything_without_sitemap(settings, corpus):
    dates, known = corpus
    site = Throttled(dates)
    summary = run_extraction(settings, SOURCES, run_id="recheck", recheck_all=True,
                             pipeline_factory=factory(FakeChatGenerator(), site))
    assert sorted(set(site.detail_calls(known))) == sorted(known)
    assert summary["skipped_no_sitemap"] == 0


def detector(tmp_path, *entries):
    d = ChangeDetector(output_dir=str(tmp_path))
    d._store = LocalProjectStore(tmp_path)
    for e in entries:
        d._store.put(e)
    return d


def detail_entry(pid, lastmod=DATE, content_hash="sha256:x"):
    return StoreEntry(project_id=pid, file="f", detail_page=True, content_hash=content_hash, first_seen="d",
                      last_updated="d", sitemap_lastmod=lastmod, source_urls=[f"https://x/{pid}_en"])


def ref(pid, lastmod=None):
    return ProjectRef(project_id=pid, title=pid, programme="TSI", year=2025, detail_url=f"https://x/{pid}_en",
                      sitemap_lastmod=lastmod)


@pytest.mark.parametrize("status", ["stale", "unavailable"])
def test_detector_without_fresh_sitemap_fetches_only_new_or_undated_pages(tmp_path, status):
    d = detector(tmp_path, detail_entry("known"), detail_entry("undated", lastmod=None),
                 detail_entry("fresh-date"))
    scope = {"countries": [], "srsp": False, "all_countries": False, "sitemap": False, "sitemap_status": status}
    with runlog.open_run(tmp_path / "logs", "r") as run:
        out = d.run(refs=[ref("known"), ref("undated"), ref("new"), ref("fresh-date", "2026-01-01T00:00Z")],
                    run_id="r", scope=scope)
    assert sorted(r.project_id for r in out["detail"]) == ["fresh-date", "new", "undated"]
    assert [r.project_id for r in out["skipped"]] == ["known"] and out["unchanged_no_sitemap"] == 1
    assert run.counts["classify:unchanged_no_sitemap"] == 1


@pytest.mark.parametrize("status", [None, "fresh", "not_requested"])
def test_detector_with_fresh_or_no_requested_sitemap_keeps_fetching_undated_pages(tmp_path, status):
    d = detector(tmp_path, detail_entry("known"))
    scope = {"countries": [], "srsp": False, "all_countries": False, "sitemap_status": status}
    out = d.run(refs=[ref("known")], run_id="r", scope=scope)
    assert [r.project_id for r in out["detail"]] == ["known"] and out["unchanged_no_sitemap"] == 0


# --- copies anciennes : decouverte seulement, jamais de date -----------------------------------------------------

def test_stale_sitemap_pages_feed_discovery_but_never_dates(tmp_path):
    known_url, new_url = f"{BASE}/health-and-long-term-care/known-page_en", f"{BASE}/green-transition/new-page_en"
    store = LocalProjectStore(tmp_path)
    store.put(StoreEntry(project_id="known-page", file="f", detail_page=True, programme="TSI", year=2022,
                         title="Known page", source_urls=[known_url, SITEMAP], first_seen="d", last_updated="d",
                         sitemap_lastmod=DATE))
    disc = ProjectDiscoverer()
    disc._store = store
    disc._fetcher = FakeFetcher(pages={new_url: fixture_bytes("detail_hems_croatia.html")})
    stale = RawPage(kind="sitemap_stale", key=SITEMAP, url="snapshot",
                    content=urlset_xml({known_url: DATE, new_url: DATE}))
    with runlog.open_run(tmp_path / "logs", "r") as run:
        out = disc.run(pages=[stale], sitemap_status="stale")
    refs = {r.project_id: r for r in out["refs"]}
    assert set(refs) == {"known-page", "new-page"}
    assert all(r.sitemap_lastmod is None for r in refs.values())  # date de la copie ancienne jamais reprise
    assert disc._fetcher.calls == [new_url]  # page connue de l'etat : pas telechargee
    assert run.counts["sitemap:known"] == 1
    assert out["scope"]["sitemap"] is False and SITEMAP not in out["scope"]["fetched"]


def test_stale_lastmod_never_skips_a_page_end_to_end(settings, corpus):
    """Le cache HTTP garde l'ancien sitemap (memes dates que l'etat) : la page n'est pas sautee « par le sitemap »."""
    dates, known = corpus
    site = Throttled(dates)
    site.cached_pages = {SITEMAP: urlset_xml(dates)}
    summary = run_extraction(settings, SOURCES, run_id="stale", pipeline_factory=factory(FakeChatGenerator(), site))
    assert summary["skipped_by_sitemap"] == 0 and "classify:unchanged_sitemap" not in summary["counts"]
    assert summary["sitemap_status"] == "stale"


def test_snapshot_is_persisted_and_used_for_discovery(settings, corpus):
    dates, known = corpus
    snapshot = json.loads((Path(settings.output_dir) / SNAPSHOT_PATH).read_text(encoding="utf-8"))
    assert snapshot["urls"][ONLY_IN_SITEMAP] == DATE and snapshot["fetched_at"]
    site = Throttled(dates)
    summary = run_extraction(settings, SOURCES, run_id="snap", pipeline_factory=factory(FakeChatGenerator(), site))
    assert summary["counts"]["sitemap:snapshot_used"] == 1 and summary["sitemap_status"] == "stale"
    # La page vue seulement dans le sitemap est decouverte (depuis l'etat, sans telechargement) et non relue.
    assert summary["skipped_no_sitemap"] == dated(settings) and site.detail_calls(known) == []
    assert ONLY_IN_SITEMAP not in site.calls


def test_known_sitemap_only_pages_are_not_downloaded_for_discovery(settings, corpus):
    dates, known = corpus
    site = Site(dates)
    summary = run_extraction(settings, SOURCES, run_id="inc", pipeline_factory=factory(FakeChatGenerator(), site))
    assert ONLY_IN_SITEMAP not in site.calls
    assert summary["skipped_by_sitemap"] == dated(settings) and summary["counts"]["sitemap:known"] == 1
    assert cli.run_exit_code(summary) == 0


# --- code de sortie et politique du workflow ----------------------------------------------------------------------

@pytest.mark.parametrize("summary, code", [
    ({"quota_exceeded": True, "sitemap_status": "unavailable"}, 1),
    ({"sitemap_status": "unavailable"}, 3), ({"sitemap_status": "stale"}, 3),
    ({"sitemap_status": "fresh"}, 0), ({"sitemap_status": "not_requested"}, 0), ({}, 0)])
def test_run_exit_code(summary, code):
    assert cli.run_exit_code(summary) == code


def test_cli_run_returns_3_when_the_sitemap_could_not_be_read(monkeypatch):
    monkeypatch.setattr(cli, "_check_llm", lambda settings: True)
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: {"sitemap_status": "unavailable"})
    assert cli.main(["run"]) == 3


RUN_STEP = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "run_step.sh"


def run_step(tmp_path, policy, code, retry=False):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash absent")
    env = {**os.environ, "CI_WORK": str(tmp_path / "work"), "GITHUB_ENV": str(tmp_path / "env")}
    if retry:
        env["STEP_RETRY_AFTER_S"] = "0"
    out = subprocess.run([bash, RUN_STEP.as_posix(), "run", policy, bash, "-c", f"echo x; exit {code}"],
                         env=env, capture_output=True, text=True)
    github_env = (tmp_path / "env").read_text() if (tmp_path / "env").exists() else ""
    return out, github_env


@pytest.mark.parametrize("policy", ["quota", "soft"])
def test_run_step_defers_exit_3_without_retry(tmp_path, policy):
    out, github_env = run_step(tmp_path, policy, 3, retry=True)
    assert out.returncode == 0 and "run (exit 3)" in github_env and "sitemap" in out.stdout
    assert "new attempt" not in out.stdout


def test_run_step_strict_fails_on_exit_3(tmp_path):
    out, _ = run_step(tmp_path, "strict", 3)
    assert out.returncode == 3
