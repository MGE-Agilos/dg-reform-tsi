"""Mode incremental : la date <lastmod> du sitemap evite de retelecharger les pages detail inchangees."""

import json
from pathlib import Path

import pytest

import tsi_agent.components.discoverer as discoverer_mod
from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from tsi_agent import __main__ as cli
from tsi_agent import runlog
from tsi_agent.components.change_detector import ChangeDetector
from tsi_agent.components.discoverer import ProjectDiscoverer, combine
from tsi_agent.components.writer import MarkdownProjectWriter
from tsi_agent.config import Settings, load_sources
from tsi_agent.models import ProjectContent, ProjectRecord, ProjectRef, RawPage
from tsi_agent.parsers.sitemap import not_modified_since, sitemap_lastmods
from tsi_agent.pipeline import build_pipeline
from tsi_agent.runner import run_extraction, seed_sitemap_lastmod
from tsi_agent.store import LocalProjectStore, StoreEntry
from tsi_agent.urls import is_canonical_detail, is_detail_candidate

SITEMAP = "https://reforms-investments.ec.europa.eu/sitemap.xml"
PAGE1 = "https://reforms-investments.ec.europa.eu/sitemap.xml_en?page=1"
BASE = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"
ROMANIA = f"{BASE}/competitiveness/comprehensive-redesign-licensing-system-romania_en"
POLAND = f"{BASE}/digital-transition/modernization-national-data-information-infrastructure-statistics-poland_en"
SCOPE = {"countries": ["FR"], "srsp": False, "all_countries": False}


# --- analyse et comparaison des dates ------------------------------------------------------------------------------

def test_sitemap_lastmods_reads_dates_of_pages_that_have_one():
    dates = sitemap_lastmods(fixture_bytes("sitemap_page.xml"))
    assert dates[ROMANIA] == "2023-04-26T14:21Z"
    assert dates[POLAND] == "2023-01-30T16:40Z"
    assert "https://reforms-investments.ec.europa.eu/index_en" not in dates  # pas de <lastmod>
    assert len(dates) == 12


def test_sitemap_lastmods_of_an_index_is_empty_and_non_xml_is_rejected():
    assert sitemap_lastmods(fixture_bytes("sitemap_index.xml")) == {}
    with pytest.raises(ValueError):
        sitemap_lastmods(b"<html>maintenance</html")


@pytest.mark.parametrize("current, recorded, expected", [
    ("2025-12-01T15:25Z", "2025-12-01T15:25Z", True),            # egale
    ("2025-11-30T08:00Z", "2025-12-01T15:25Z", True),            # anterieure
    ("2025-12-01T15:26Z", "2025-12-01T15:25Z", False),           # plus recente
    ("2025-12-01T15:25:00+00:00", "2025-12-01T15:25Z", True),    # formats differents, meme instant
    ("2025-12-01T16:25+01:00", "2025-12-01T15:25Z", True),       # fuseaux
    ("2025-12-01", "2025-12-01T15:25Z", True),                   # date seule : minuit UTC
    (None, "2025-12-01T15:25Z", False),                          # pas de date dans le sitemap
    ("2025-12-01T15:25Z", None, False),                          # pas de date dans l'etat
    ("garbage", "2025-12-01T15:25Z", False),                     # illisible : on telecharge
])
def test_not_modified_since(current, recorded, expected):
    assert not_modified_since(current, recorded) is expected


# --- decouverte : la date suit la page detail ------------------------------------------------------------------------

def test_discoverer_puts_the_sitemap_date_on_refs_with_a_detail_page(monkeypatch):
    monkeypatch.setattr(discoverer_mod, "parse_srsp_pdf", lambda content, url: [])
    spotlight = (f'<main><article class="ecl-content-item"><img src="/sites/x/Poland_flag_photo.jpg">'
                 f'<a href="{POLAND}">Modernization of the national data-information infrastructure - Statistics '
                 f'Poland</a></article></main>').encode()
    pages = [RawPage(kind="spotlight", key="spotlight", url=f"{BASE}/our-projects/projects-spotlight_en",
                     content=spotlight),
             RawPage(kind="sitemap", key=SITEMAP, url=PAGE1, content=fixture_bytes("sitemap_page.xml"))]
    disc = ProjectDiscoverer()
    disc._fetcher = FakeFetcher(pages={ROMANIA: fixture_bytes("detail_licensing_romania.html")},
                                default=lambda url: b"<html><main></main></html>")
    refs = {r.project_id: r for r in disc.run(pages=pages)["refs"]}
    assert refs["modernization-national-data-information-infrastructure-statistics-poland"].sitemap_lastmod == \
        "2023-01-30T16:40Z"  # page detail vue dans un listing
    assert refs["comprehensive-redesign-licensing-system-romania"].sitemap_lastmod == "2023-04-26T14:21Z"


def test_discoverer_without_sitemap_leaves_dates_empty():
    html = (f'<main><article class="ecl-content-item"><img src="/sites/x/Poland_flag_photo.jpg">'
            f'<a href="{POLAND}">Statistics Poland</a></article></main>').encode()
    disc = ProjectDiscoverer()
    disc._fetcher = FakeFetcher()
    refs = disc.run(pages=[RawPage(kind="spotlight", key="spotlight", url="https://x/spot_en", content=html)])["refs"]
    assert [r.sitemap_lastmod for r in refs] == [None]


def test_combine_keeps_the_sitemap_date_and_listing_hash_ignores_it():
    detail = ProjectRef(project_id="p", title="P", detail_url="https://x/p_en", sitemap_lastmod="2025-01-01T00:00Z")
    thin = ProjectRef(project_id="tsi-2022-p", title="P")
    assert combine(thin, detail).sitemap_lastmod == "2025-01-01T00:00Z"
    assert combine(detail, thin).sitemap_lastmod == "2025-01-01T00:00Z"
    assert detail.listing_hash() == detail.model_copy(update={"sitemap_lastmod": "2026-01-01"}).listing_hash()


# --- detecteur : pas de telechargement pour une page connue inchangee ------------------------------------------------

def detail(pid, lastmod=None):
    return ProjectRef(project_id=pid, title=pid, programme="TSI", year=2025, countries=["FR"],
                      detail_url=f"https://x/{pid}_en", sitemap_lastmod=lastmod)


def detector_with(tmp_path, entries):
    d = ChangeDetector(output_dir=str(tmp_path))
    d._store = LocalProjectStore(tmp_path)
    for ref, lastmod in entries:
        d._store.put(StoreEntry(project_id=ref.project_id, file="f", programme="TSI", detail_page=True,
                                content_hash="sha256:x", listing_hash=ref.listing_hash(), countries=["FR"],
                                first_seen="d", last_updated="d", sitemap_lastmod=lastmod))
    return d


def test_known_page_with_same_or_older_date_is_skipped(tmp_path):
    same, older = detail("same", "2025-12-01T15:25Z"), detail("older", "2025-01-01T00:00Z")
    d = detector_with(tmp_path, [(same, "2025-12-01T15:25Z"), (older, "2025-12-01T15:25Z")])
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        out = d.run(refs=[same, older], run_id="r1", scope=SCOPE)
    assert out["detail"] == [] and [r.project_id for r in out["skipped"]] == ["same", "older"]
    assert run.counts["classify:unchanged_sitemap"] == 2
    # Proposees une seule fois par run : un lot suivant ne les recompte pas.
    assert d.run(refs=[same, older], run_id="r1", scope=SCOPE)["skipped"] == []


@pytest.mark.parametrize("ref_date, stored_date", [
    ("2025-12-02T00:00Z", "2025-12-01T15:25Z"),  # plus recente
    (None, "2025-12-01T15:25Z"),                 # pas de date dans le sitemap
    ("2025-12-01T15:25Z", None),                 # pas de date dans l'etat
])
def test_newer_or_missing_date_is_fetched(tmp_path, ref_date, stored_date):
    ref = detail("p", ref_date)
    d = detector_with(tmp_path, [(ref, stored_date)])
    out = d.run(refs=[ref], run_id="r", scope=SCOPE)
    assert [r.project_id for r in out["detail"]] == ["p"] and out["skipped"] == []


def test_new_page_and_full_are_fetched(tmp_path):
    known = detail("known", "2025-12-01T15:25Z")
    d = detector_with(tmp_path, [(known, "2025-12-01T15:25Z")])
    out = d.run(refs=[known, detail("new", "2025-12-01T15:25Z")], run_id="r", scope=SCOPE, full=True)
    assert [r.project_id for r in out["detail"]] == ["known", "new"] and out["skipped"] == []
    out = d.run(refs=[known, detail("new", "2025-12-01T15:25Z")], run_id="r2", scope=SCOPE)
    assert [r.project_id for r in out["detail"]] == ["new"]


def test_sitemap_skip_can_be_disabled_without_full(tmp_path):
    known = detail("known", "2025-12-01T15:25Z")
    d = detector_with(tmp_path, [(known, "2025-12-01T15:25Z")])
    out = d.run(refs=[known], run_id="r", scope=SCOPE, sitemap_skip=False)
    assert [r.project_id for r in out["detail"]] == ["known"] and out["skipped"] == []


def test_skipped_pages_do_not_count_against_the_limit(tmp_path):
    a, b = detail("a", "2025-01-01T00:00Z"), detail("b", "2025-01-01T00:00Z")
    d = detector_with(tmp_path, [(a, "2025-01-01T00:00Z"), (b, "2025-01-01T00:00Z")])
    out = d.run(refs=[a, b, detail("c"), detail("d")], run_id="r", scope=SCOPE, limit=1)
    assert [r.project_id for r in out["detail"]] == ["c"] and out["remaining"] == 1
    assert [r.project_id for r in out["skipped"]] == ["a", "b"]


# --- writer : enregistrement de la date ------------------------------------------------------------------------------

URL = f"{BASE}/health-and-long-term-care/hems_en"


def ref(**kw):
    base = dict(project_id="hems", title="HEMS", programme="TSI", year=2022, countries=["HR"], detail_url=URL,
                listing_urls=["https://x/croatia_en"])
    return ProjectRef(**{**base, **kw})


def record(r):
    return ProjectRecord(ref=r, listing_hash=r.listing_hash(), content=ProjectContent(summary="S."),
                         content_hash="sha256:1", model="m", intro="I.", sections=[("Context", "C.")])


def writer(tmp_path):
    w = MarkdownProjectWriter()
    w._store = LocalProjectStore(tmp_path)
    return w


def test_writer_records_the_date_of_written_and_unchanged_pages(tmp_path):
    w = writer(tmp_path)
    w.run(run_id="r1", records=[record(ref(sitemap_lastmod="2025-01-01T00:00Z"))])
    assert w._store.get("hems").sitemap_lastmod == "2025-01-01T00:00Z"
    # Page retelechargee mais inchangee (listing identique) : la nouvelle date est quand meme enregistree.
    out = w.run(run_id="r2", unchanged=[ref(sitemap_lastmod="2025-06-01T00:00Z")])
    assert out["stats"]["refreshed"] == 0
    assert LocalProjectStore(tmp_path).get("hems").sitemap_lastmod == "2025-06-01T00:00Z"
    # Run sans sitemap (pilote) : la date enregistree n'est pas effacee.
    w.run(run_id="r3", unchanged=[ref()])
    w.run(run_id="r4", records=[record(ref())])
    assert LocalProjectStore(tmp_path).get("hems").sitemap_lastmod == "2025-06-01T00:00Z"


def test_writer_refreshes_listing_metadata_of_skipped_pages(tmp_path):
    w = writer(tmp_path)
    w.run(run_id="r1", records=[record(ref(sitemap_lastmod="2025-01-01T00:00Z"))])
    out = w.run(run_id="r2", skipped=[ref(countries=["HR", "SI"], sitemap_lastmod="2025-01-01T00:00Z")])
    assert out["stats"]["refreshed"] == 1
    entry = w._store.get("hems")
    assert entry.countries == ["HR", "SI"] and entry.sitemap_lastmod == "2025-01-01T00:00Z"


# --- runner et CLI : de bout en bout ----------------------------------------------------------------------------------

SOURCES = load_sources("config/sources.yaml")
HR = SOURCES["factsheet_url"].format(slug="croatia")


def fake_site(url: str) -> bytes | None:
    if url == HR:
        return fixture_bytes("factsheet_croatia.html")
    if url == SOURCES["spotlight_url"]:
        return fixture_bytes("spotlight.html")
    if url.endswith("/green-transition_en"):
        return fixture_bytes("policy_area_green_transition.html")
    if "/technical-support-instrument-0/" in url and url.endswith("_en") and "tsi-country-factsheets" not in url:
        if url.count("/") <= 4:
            return b"<html><main></main></html>"
        return fixture_bytes("detail_hems_croatia.html")
    return None


def urlset(dates: dict[str, str]) -> bytes:
    body = "".join(f"<url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in dates.items())
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>").encode()


class Site(FakeFetcher):
    """Site factice : le sitemap (simple, sans index) donne `dates` ; journalise les pages detail lues."""

    def __init__(self, dates: dict[str, str]):
        super().__init__(default=self._page)
        self.dates = dates

    def _page(self, url):
        return urlset(self.dates) if url == SOURCES["sitemap_url"] else fake_site(url)

    def detail_calls(self, urls):
        # Les liens non canoniques sont resolus a chaque run par la decouverte (inchange) : hors du comptage.
        return [u for u in self.calls if u in urls and is_canonical_detail(u)]


@pytest.fixture
def settings(tmp_path):
    return Settings(output_dir=str(tmp_path / "output"), logs_dir=str(tmp_path / "logs"),
                    cache_dir=str(tmp_path / "cache"), min_interval_s=0, chunk_size=20)


def factory(generator, fetcher):
    def make(settings, sources, dry_run=False, model=None):
        pipe = build_pipeline(settings, sources, dry_run=dry_run, chat_generator=generator,
                              judge_generator=FakeChatGenerator())
        for name in ("listing", "discoverer", "judge", "detail_fetcher"):
            if name in pipe.graph.nodes:
                pipe.get_component(name)._fetcher = fetcher
        return pipe
    return make


def detail_urls(settings) -> list[str]:
    store = LocalProjectStore(settings.output_dir)
    return sorted({u for e in store.all().values() if e.detail_page for u in e.source_urls
                   if is_detail_candidate(u) and u.endswith("_en") and "/technical-support-instrument-0/" in u
                   and u.count("/") > 4})


def test_seed_then_incremental_run_then_newer_date_then_full(settings):
    # Corpus initial, sans sitemap (run pilote) : aucune date.
    run_extraction(settings, SOURCES, countries=["HR"], run_id="init",
                   pipeline_factory=factory(FakeChatGenerator(), Site({})))
    urls = detail_urls(settings)
    assert len(urls) > 40
    dates = {u: "2025-12-01T15:25Z" for u in urls}

    # Initialisation : dates enregistrees sans telecharger les pages detail.
    site = Site(dates)
    seeded = seed_sitemap_lastmod(settings, SOURCES, fetcher=site, run_id="seed")
    assert seeded["seeded"] == len(urls) and seeded["mode"] == "seed_sitemap_lastmod"
    assert site.detail_calls(urls) == []
    store = LocalProjectStore(settings.output_dir)
    assert all(e.sitemap_lastmod == "2025-12-01T15:25Z" for e in store.all().values()
               if e.detail_page and any(u in dates for u in e.source_urls))

    # Run incremental : aucune page detail relue, aucun appel LLM.
    site, gen = Site(dates), FakeChatGenerator()
    summary = run_extraction(settings, SOURCES, run_id="inc", pipeline_factory=factory(gen, site))
    assert summary["skipped_by_sitemap"] == len(urls) and summary["llm_calls"] == 0 and gen.calls == []
    assert site.detail_calls(urls) == []
    saved = json.loads(Path(settings.logs_dir, "runs", "inc.summary.json").read_text(encoding="utf-8"))
    assert saved["skipped_by_sitemap"] == len(urls)

    # Une page a une date plus recente : elle seule est relue ; inchangee, sa nouvelle date est enregistree.
    newer = urls[0]
    site = Site({**dates, newer: "2026-01-01T00:00Z"})
    summary = run_extraction(settings, SOURCES, run_id="newer", pipeline_factory=factory(FakeChatGenerator(), site))
    assert site.detail_calls(urls) == [newer] and summary["skipped_by_sitemap"] == len(urls) - 1
    entry = next(e for e in LocalProjectStore(settings.output_dir).all().values() if newer in e.source_urls)
    assert entry.sitemap_lastmod == "2026-01-01T00:00Z"

    # --recheck-all : tout est relu, mais seul le texte change part au LLM (ici : rien).
    site, gen = Site(dates), FakeChatGenerator()
    summary = run_extraction(settings, SOURCES, recheck_all=True, run_id="recheck",
                             pipeline_factory=factory(gen, site))
    assert sorted(set(site.detail_calls(urls))) == [u for u in urls if is_canonical_detail(u)]
    assert summary["skipped_by_sitemap"] == 0 and summary["llm_calls"] == 0 and gen.calls == []

    # --full : tout est relu.
    site = Site(dates)
    summary = run_extraction(settings, SOURCES, full=True, run_id="full",
                             pipeline_factory=factory(FakeChatGenerator(), site))
    assert sorted(set(site.detail_calls(urls))) == [u for u in urls if is_canonical_detail(u)]
    assert summary["skipped_by_sitemap"] == 0


def test_seed_fails_cleanly_without_sitemap(settings):
    with pytest.raises(RuntimeError, match="sitemap"):
        seed_sitemap_lastmod(settings, SOURCES, fetcher=FakeFetcher(), run_id="s")


def test_cli_passes_recheck_all(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_check_llm", lambda settings: True)
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: seen.update(k) or {"quota_exceeded": False})
    assert cli.main(["run", "--recheck-all"]) == 0
    assert seen["recheck_all"] is True and seen["full"] is False
    assert cli.main(["run"]) == 0 and seen["recheck_all"] is False


def test_cli_seed_option_needs_no_llm(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli, "_check_llm", lambda settings: pytest.fail("pas de LLM pour l'initialisation"))
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: pytest.fail("pas d'extraction"))

    def fake_seed(settings, sources, **kw):
        seen["called"] = True
        return {"mode": "seed_sitemap_lastmod", "seeded": 3}

    monkeypatch.setattr(cli, "seed_sitemap_lastmod", fake_seed)
    assert cli.main(["run", "--seed-sitemap-lastmod"]) == 0
    assert seen["called"] and '"seeded": 3' in capsys.readouterr().out

    def broken(settings, sources, **kw):
        raise RuntimeError("sitemap incomplete")

    monkeypatch.setattr(cli, "seed_sitemap_lastmod", broken)
    assert cli.main(["run", "--seed-sitemap-lastmod"]) == 1
    assert "sitemap incomplete" in capsys.readouterr().err
