"""Doublons thin d'une page detail connue : run du 2026-10-19 (sitemap en HTTP 429, cache vide, --recheck-all).

Le sitemap indisponible, les pages detail trouvees seulement par lui n'etaient pas decouvertes : leurs projets
sans page detail (PDF SRSP, fiches pays), alias connus de ces pages dans l'etat, etaient ecrits en thin.
"""

import pytest

import tsi_agent.components.discoverer as discoverer_mod
from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.change_detector import ChangeDetector
from tsi_agent.components.discoverer import ProjectDiscoverer
from tsi_agent.components.writer import MarkdownProjectWriter
from tsi_agent.config import Settings
from tsi_agent.http import FetchError
from tsi_agent.models import ProjectRef, RawPage
from tsi_agent.pipeline import build_pipeline
from tsi_agent.render import front_matter, render
from tsi_agent.runner import run_extraction
from tsi_agent.store import LocalProjectStore, StoreEntry

SITE = "https://reforms-investments.ec.europa.eu"
SITEMAP = f"{SITE}/sitemap.xml"
PDF = "https://commission.europa.eu/system/files/2020-03/rss-technical-support-per-country_en.pdf"
OWNER_URL = f"{SITE}/technical-support-instrument-0/health-and-long-term-care/hems-croatia_en"
OWNER = "hems-croatia"
THIN = "srsp-2017-support-for-the-modernisation-of-air-rescue"  # titre sans rapport : ni cle titre ni flou
LASTMOD = "2024-07-11T08:08Z"
SOURCES = {"factsheet_url": f"{SITE}/factsheets/{{slug}}_en", "spotlight_url": f"{SITE}/spotlight_en",
           "policy_area_url": f"{SITE}/area/{{slug}}_en", "policy_areas": [], "srsp_pdf_url": PDF,
           "sitemap_url": SITEMAP, "programme_docs": []}


def thin_ref(**kw):
    return ProjectRef(project_id=THIN, title="Support for the modernisation of air rescue", programme="SRSP",
                      year=2017, countries=["HR"], listing_urls=[PDF], **kw)


def owner_entry(**kw):
    values = dict(project_id=OWNER, file=f"projects/{OWNER}.md", programme="SRSP", detail_page=True,
                  content_hash="sha256:x", listing_hash="l", source_urls=[OWNER_URL, SITEMAP, PDF],
                  countries=["HR"], first_seen="2026-01-01", last_updated="2026-01-01", aliases=[THIN],
                  sitemap_lastmod=LASTMOD)  # etat ancien : ni titre ni annee (indices de la fusion floue absents)
    values.update(kw)
    return StoreEntry(**values)


def thin_entry(pid=THIN):
    return StoreEntry(project_id=pid, file=f"projects/{pid}.md", programme="SRSP", detail_page=False,
                      listing_hash="t", source_urls=[PDF], countries=["HR"], first_seen="2026-10-19",
                      last_updated="2026-10-19")


# --- decouverte : fusion deterministe par l'alias connu ----------------------------------------------------------

def urlset(dates):
    body = "".join(f"<url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in dates.items())
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>").encode()


def test_discoverer_merges_a_known_alias_into_its_detail_page_without_hints(tmp_path, monkeypatch):
    monkeypatch.setattr(discoverer_mod, "parse_srsp_pdf", lambda content, url: [thin_ref()])
    store = LocalProjectStore(tmp_path)
    store.put(owner_entry())
    disc = ProjectDiscoverer()
    disc._store = store
    disc._fetcher = FakeFetcher(pages={OWNER_URL: fixture_bytes("detail_hems_croatia.html")})  # cache HTTP vide
    pages = [RawPage(kind="srsp_pdf", key="srsp", url=PDF, content=b"%PDF"),
             RawPage(kind="sitemap", key=SITEMAP, url=SITEMAP, content=urlset({OWNER_URL: LASTMOD}))]
    with runlog.open_run(tmp_path / "logs", "r") as run:
        out = disc.run(pages=pages)
    [ref] = out["refs"]
    assert ref.project_id == OWNER and THIN in ref.aliases and PDF in ref.listing_urls
    assert run.counts["discover:alias_merge"] == 1 and "discover:fuzzy_merge" not in run.counts
    assert out["scope"]["sitemap"] is True


def test_discoverer_never_attaches_an_ambiguous_title_key(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.put(owner_entry(aliases=["tsi-2022-flagship"]))
    disc = ProjectDiscoverer()
    disc._store = store
    detail = ProjectRef(project_id=OWNER, title="HEMS", detail_url=OWNER_URL)
    flagship = ProjectRef(project_id="tsi-2022-flagship", title="Flagship")
    assert len(disc._merge_known_aliases([detail, flagship], {"tsi-2022-flagship"})) == 2
    assert len(disc._merge_known_aliases([detail, flagship], set())) == 1


# --- detecteur : sans sitemap, l'alias d'une page detail n'est pas ecrit en thin ----------------------------------

def detector(tmp_path, *entries):
    d = ChangeDetector(output_dir=str(tmp_path))
    d._store = LocalProjectStore(tmp_path)
    for e in entries:
        d._store.put(e)
    return d


def test_alias_of_an_undiscovered_detail_page_is_not_written_when_the_sitemap_was_not_read(tmp_path):
    d = detector(tmp_path, owner_entry())
    scope = {"countries": [], "srsp": True, "all_countries": False, "sitemap": False, "fetched": [PDF]}
    out = d.run(refs=[thin_ref()], run_id="r", scope=scope)
    assert out["thin"] == [] and out["delisted"] == []


def test_alias_comes_back_as_thin_when_the_sitemap_read_shows_the_page_vanished(tmp_path):
    d = detector(tmp_path, owner_entry())
    scope = {"countries": [], "srsp": True, "all_countries": False, "sitemap": True, "fetched": [PDF, SITEMAP]}
    out = d.run(refs=[thin_ref()], run_id="r", scope=scope)
    assert [r.project_id for r in out["thin"]] == [THIN] and out["delisted"] == [OWNER]


def test_existing_duplicate_thin_entry_is_not_rewritten(tmp_path):
    d = detector(tmp_path, owner_entry(), thin_entry())
    scope = {"countries": [], "srsp": True, "all_countries": False, "sitemap": False, "fetched": [PDF]}
    assert d.run(refs=[thin_ref(highlighted=True)], run_id="r", scope=scope)["thin"] == []


# --- writer : reparation des doublons deja publies ----------------------------------------------------------------

def writer_with(tmp_path, *entries):
    w = MarkdownProjectWriter(output_dir=str(tmp_path))
    w._store = LocalProjectStore(tmp_path)
    for e in entries:
        w._store.put(e)
        w._store.write_project_file(e.project_id, "---\ntitle: x\n---\n")
    return w


def test_writer_removes_thin_entries_whose_id_is_an_alias_of_a_listed_detail_page(tmp_path):
    other = owner_entry(project_id="other", aliases=["tsi-2021-other"], source_urls=[])
    w = writer_with(tmp_path, owner_entry(), thin_entry(), other, thin_entry("tsi-2021-other"), thin_entry("alone"))
    with runlog.open_run(tmp_path / "logs", "r") as run:
        out = w.run(run_id="r")
    assert out["stats"]["duplicates_removed"] == 2 and run.counts["write:duplicate_removed"] == 2
    store = LocalProjectStore(tmp_path)
    assert sorted(store.all()) == ["alone", OWNER, "other"]
    assert not (tmp_path / "projects" / f"{THIN}.md").exists()
    assert (tmp_path / "projects" / "alone.md").exists()
    assert w.run(run_id="r2")["stats"]["duplicates_removed"] == 0


def test_writer_keeps_the_thin_of_a_delisted_detail_page_and_detail_aliases(tmp_path):
    second_detail = owner_entry(project_id="second", aliases=[], source_urls=[])
    w = writer_with(tmp_path, owner_entry(listed=False), thin_entry(),
                    owner_entry(project_id="third", aliases=["second"], source_urls=[]), second_detail)
    assert w.run(run_id="r")["stats"]["duplicates_removed"] == 0
    assert sorted(LocalProjectStore(tmp_path).all()) == sorted(["second", THIN, "third", OWNER])


# --- de bout en bout, conditions du runner GitHub Actions ---------------------------------------------------------

class CiSite(FakeFetcher):
    """Cache HTTP vide ; sitemap en HTTP 429 (`throttled`) ; journalise les pages detail lues."""

    def __init__(self, throttled: bool):
        super().__init__(pages={OWNER_URL: fixture_bytes("detail_hems_croatia.html"), PDF: b"%PDF",
                                SITEMAP: urlset({OWNER_URL: LASTMOD})})
        self.throttled = throttled

    def get(self, url, cache=True):
        if self.throttled and url == SITEMAP:
            self.calls.append(url)
            raise FetchError(url, "HTTP 429", 429)
        return super().get(url, cache)


def factory(site):
    def make(settings, sources, dry_run=False, model=None):
        pipe = build_pipeline(settings, sources, dry_run=dry_run, chat_generator=FakeChatGenerator(),
                              judge_generator=FakeChatGenerator())
        for name in ("listing", "discoverer", "judge", "detail_fetcher"):
            if name in pipe.graph.nodes:
                pipe.get_component(name)._fetcher = site
        return pipe
    return make


@pytest.fixture
def ci(tmp_path, monkeypatch):
    monkeypatch.setattr(discoverer_mod, "parse_srsp_pdf", lambda content, url: [thin_ref()])
    s = Settings(output_dir=str(tmp_path / "output"), logs_dir=str(tmp_path / "logs"),
                 cache_dir=str(tmp_path / "cache"), min_interval_s=0, chunk_size=20)
    store = LocalProjectStore(s.output_dir)  # etat restaure de la release precedente
    store.put(owner_entry())
    store.write_project_file(OWNER, render(front_matter(
        ProjectRef(project_id=OWNER, title="HEMS", programme="SRSP", year=2017, countries=["HR"],
                   detail_url=OWNER_URL, aliases=[THIN]),
        content_hash="sha256:x", first_seen="2026-01-01", last_updated="2026-01-01", run_id="old", model="m"),
        "body\n"))
    return s


@pytest.mark.parametrize("throttled", [True, False])
@pytest.mark.parametrize("recheck_all", [True, False])
def test_ci_run_never_creates_a_thin_duplicate_of_a_known_detail_page(ci, throttled, recheck_all):
    summary = run_extraction(ci, SOURCES, run_id="ci", recheck_all=recheck_all,
                             pipeline_factory=factory(CiSite(throttled)))
    store = LocalProjectStore(ci.output_dir)
    assert sorted(store.all()) == [OWNER]
    assert store.get(OWNER).listed and THIN in store.get(OWNER).aliases
    assert summary["written"]["thin"] == 0 and summary["counts"].get("classify:new_thin", 0) == 0


def test_ci_run_removes_the_duplicates_published_by_the_faulty_run(ci):
    store = LocalProjectStore(ci.output_dir)
    store.put(thin_entry())
    store.write_project_file(THIN, "---\ntitle: thin\n---\n")
    summary = run_extraction(ci, SOURCES, run_id="ci", pipeline_factory=factory(CiSite(throttled=True)))
    assert summary["duplicates_removed"] == 1 and summary["counts"]["write:duplicate_removed"] == 1
    assert sorted(LocalProjectStore(ci.output_dir).all()) == [OWNER]
    assert not (LocalProjectStore(ci.output_dir).projects_dir / f"{THIN}.md").exists()
