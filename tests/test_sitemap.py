"""Decouverte par le sitemap du site : parsing, filtrage des URLs, pages detail manquantes, fusion avec les thin."""

import pytest

import tsi_agent.components.discoverer as discoverer_mod
from conftest import FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.discoverer import ProjectDiscoverer
from tsi_agent.components.listing import SourceListingFetcher
from tsi_agent.config import load_sources
from tsi_agent.http import FetchError
from tsi_agent.models import ProjectRef, RawPage
from tsi_agent.parsers.sitemap import DEFAULT_EXCLUDED_AREAS, parse_sitemap, sitemap_project_urls

SITEMAP = "https://reforms-investments.ec.europa.eu/sitemap.xml"
PAGE1 = "https://reforms-investments.ec.europa.eu/sitemap.xml_en?page=1"
PAGE2 = "https://reforms-investments.ec.europa.eu/sitemap.xml_en?page=2"
BASE = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"
PDF = "https://commission.europa.eu/system/files/2020-03/rss-technical-support-per-country_en.pdf"
SPOTLIGHT = f"{BASE}/our-projects/projects-spotlight_en"
ROMANIA = f"{BASE}/competitiveness/comprehensive-redesign-licensing-system-romania_en"
BIM = f"{BASE}/competitiveness/implementation-bim-roadmap-greece-including-pilot-projects_en"
MALTA = f"{BASE}/green-transition/sustainable-development-strategy-malta-2050_en"
FLAGSHIP = f"{BASE}/flagship-technical-support-projects/tsi-2025-flagship-pace-public-administration-cooperation-exchange_en"
POLAND = f"{BASE}/digital-transition/modernization-national-data-information-infrastructure-statistics-poland_en"


def test_parse_sitemap_index_and_page():
    children, urls = parse_sitemap(fixture_bytes("sitemap_index.xml"))
    assert children == [PAGE1, PAGE2] and urls == []
    children, urls = parse_sitemap(fixture_bytes("sitemap_page.xml"))
    assert children == [] and len(urls) == 13
    assert urls[0] == "https://reforms-investments.ec.europa.eu/index_en"
    assert ROMANIA in urls  # les liens hreflang (xhtml:link) ne sont pas des URLs du sitemap
    assert not any(u.endswith("_bg") for u in urls)


def test_parse_sitemap_rejects_non_xml():
    with pytest.raises(ValueError):
        parse_sitemap(b"<html>maintenance</html")


def test_sitemap_project_urls_keeps_only_project_pages_of_tsi_areas():
    _, urls = parse_sitemap(fixture_bytes("sitemap_page.xml"))
    assert sitemap_project_urls(urls, DEFAULT_EXCLUDED_AREAS) == [ROMANIA, BIM, MALTA, POLAND]
    # Sans exclusion, seules les pages a deux niveaux restent (pas les index de domaine ni les anciens chemins).
    assert sitemap_project_urls(urls, []) == [
        ROMANIA, BIM, MALTA, FLAGSHIP, POLAND, SPOTLIGHT, f"{BASE}/our-partners/international-organisations_en",
        f"{BASE}/other-eu-support-opportunities/taiex-instrument_en", f"{BASE}/what-we-do/education-and-training_en",
        f"{BASE}/our-projects-0/italy_en"]
    # Doublons retires, ordre conserve.
    assert sitemap_project_urls([MALTA, ROMANIA, MALTA], []) == [MALTA, ROMANIA]


def test_sources_config_declares_the_sitemap():
    sources = load_sources("config/sources.yaml")
    assert sources["sitemap_url"] == SITEMAP
    assert set(DEFAULT_EXCLUDED_AREAS) <= set(sources["sitemap_excluded_areas"])
    assert "flagship-technical-support-projects" in sources["sitemap_excluded_areas"]


# --- etape listing : index + pages du sitemap ---------------------------------------------------------------------

def listing(fetcher, **extra):
    sources = {"factsheet_url": "https://x/{slug}_en", "spotlight_url": "https://x/spot_en",
               "policy_area_url": "https://x/{slug}_en", "policy_areas": [], "srsp_pdf_url": "https://x/srsp.pdf",
               "sitemap_url": SITEMAP, **extra}
    comp = SourceListingFetcher(sources=sources)
    comp._fetcher = fetcher
    return comp


def test_listing_fetches_sitemap_index_and_its_pages():
    page = fixture_bytes("sitemap_page.xml")
    fetcher = FakeFetcher(pages={SITEMAP: fixture_bytes("sitemap_index.xml"), PAGE1: page, PAGE2: page},
                          default=lambda url: b"<html></html>")
    pages = listing(fetcher).run()["pages"]
    sitemap_pages = [p for p in pages if p.kind == "sitemap"]
    assert [(p.key, p.url) for p in sitemap_pages] == [(SITEMAP, PAGE1), (SITEMAP, PAGE2)]


def test_listing_skips_sitemap_in_pilot_runs_and_when_a_page_fails(tmp_path):
    fetcher = FakeFetcher(pages={SITEMAP: fixture_bytes("sitemap_index.xml")}, default=lambda url: b"<html></html>")
    assert not [p for p in listing(fetcher).run(countries=["HR"])["pages"] if p.kind == "sitemap"]
    assert SITEMAP not in fetcher.calls

    def default(url):
        return None if url == PAGE2 else b"<html></html>"

    fetcher = FakeFetcher(pages={SITEMAP: fixture_bytes("sitemap_index.xml"),
                                 PAGE1: fixture_bytes("sitemap_page.xml")}, default=default)
    with runlog.open_run(str(tmp_path), "r1"):
        pages = listing(fetcher).run()["pages"]
    assert not [p for p in pages if p.kind == "sitemap"]  # sitemap incomplet : rien plutot qu'une vue partielle
    events = (tmp_path / "runs" / "r1.jsonl").read_text(encoding="utf-8")
    assert "sitemap_incomplete" in events


def test_listing_without_sitemap_url_does_nothing_more():
    fetcher = FakeFetcher(default=lambda url: b"<html></html>")
    comp = listing(fetcher)
    del comp.sources["sitemap_url"]
    assert not [p for p in comp.run()["pages"] if p.kind == "sitemap"]


# --- etape decouverte ---------------------------------------------------------------------------------------------

SPOTLIGHT_HTML = f"""<main><article class="ecl-content-item"><img src="/sites/x/Poland_flag_photo.jpg">
<a href="{POLAND}">Modernization of the national data-information infrastructure - Statistics Poland</a></article></main>
""".encode()


def thin_refs():
    return [
        ProjectRef(project_id="srsp-2019-comprehensive-redesign-of-the-licensing-system-in-romania",
                   title="Comprehensive redesign of the licensing system in Romania", programme="SRSP", year=2019,
                   countries=["RO"], listing_urls=[PDF]),
        ProjectRef(project_id="srsp-2019-modernization-of-the-national-data-information-infrastructure-statistics-poland",
                   title="Modernization of the national data-information infrastructure - statistics Poland",
                   programme="SRSP", year=2019, countries=["PL"], listing_urls=[PDF]),
        ProjectRef(project_id="tsi-2023-implementation-of-the-bim-roadmap-in-greece-including-pilot-projects",
                   title="Implementation of the BIM Roadmap in Greece, including pilot projects.", programme="TSI",
                   year=2023, countries=["GR"], listing_urls=["https://x/greece_en"]),
    ]


@pytest.fixture
def pages(monkeypatch):
    monkeypatch.setattr(discoverer_mod, "parse_srsp_pdf", lambda content, url: thin_refs())
    sm = fixture_bytes("sitemap_page.xml")
    return [RawPage(kind="srsp_pdf", key="srsp", url=PDF, content=b"pdf"),
            RawPage(kind="spotlight", key="spotlight", url=SPOTLIGHT, content=SPOTLIGHT_HTML),
            RawPage(kind="sitemap", key=SITEMAP, url=PAGE1, content=sm),
            RawPage(kind="sitemap", key=SITEMAP, url=PAGE2, content=sm)]


def detail_fetcher(**extra):
    return FakeFetcher(pages={ROMANIA: fixture_bytes("detail_licensing_romania.html"),
                              BIM: fixture_bytes("detail_bim_greece.html"),
                              MALTA: fixture_bytes("detail_malta_2050.html"),
                              FLAGSHIP: fixture_bytes("detail_flagship_pace.html"), **extra})


def by_id(refs):
    return {r.project_id: r for r in refs}


def test_sitemap_pages_become_projects_merged_with_thin_refs(pages):
    disc = ProjectDiscoverer()
    disc._fetcher = detail_fetcher()
    out = disc.run(pages=pages)
    refs = by_id(out["refs"])
    assert sorted(refs) == ["comprehensive-redesign-licensing-system-romania",
                            "implementation-bim-roadmap-greece-including-pilot-projects",
                            "modernization-national-data-information-infrastructure-statistics-poland",
                            "sustainable-development-strategy-malta-2050"]
    # Seules les pages detail absentes des listings sont recuperees (Poland est dans le spotlight).
    assert disc._fetcher.calls == [ROMANIA, BIM, MALTA]
    ro = refs["comprehensive-redesign-licensing-system-romania"]
    assert ro.detail_url == ROMANIA and ro.aliases == ["srsp-2019-comprehensive-redesign-of-the-licensing-system-in-romania"]
    assert (ro.programme, ro.year, ro.countries, ro.policy_area) == ("SRSP", 2019, ["RO"], "competitiveness")
    assert ro.listing_urls == [PDF, SITEMAP] or ro.listing_urls == [SITEMAP, PDF]
    bim = refs["implementation-bim-roadmap-greece-including-pilot-projects"]
    assert bim.aliases == ["tsi-2023-implementation-of-the-bim-roadmap-in-greece-including-pilot-projects"]
    assert bim.title == "Implementation of the BIM roadmap in Greece, including pilot projects"
    # Nouveau projet : rien dans les listings ; pays laisses au LLM (page detail sans champ pays).
    mt = refs["sustainable-development-strategy-malta-2050"]
    assert (mt.programme, mt.year, mt.countries, mt.aliases, mt.listing_urls) == ("SRSP", 2019, [], [], [SITEMAP])
    assert mt.policy_area == "green-transition"
    # Doublon (c) : page detail du spotlight + mention thin du PDF.
    pl = refs["modernization-national-data-information-infrastructure-statistics-poland"]
    assert pl.aliases == ["srsp-2019-modernization-of-the-national-data-information-infrastructure-statistics-poland"]
    assert (pl.programme, pl.year, pl.spotlight) == ("SRSP", 2019, True)
    assert SITEMAP in out["scope"]["fetched"]


def test_discovery_report(pages):
    disc = ProjectDiscoverer()
    disc._fetcher = detail_fetcher()
    report = disc.run(pages=pages)["report"]
    assert report["refs"] == 4
    assert report["sitemap"] == {"urls": 4, "new_urls": 3, "fetched": 3, "not_project_pages": [], "failed": [],
                                 "deferred": []}
    merged = {(m["sitemap_slug"], m["thin_id"], m["rule"]) for m in report["merged_into_thin"]}
    assert merged == {
        ("comprehensive-redesign-licensing-system-romania",
         "srsp-2019-comprehensive-redesign-of-the-licensing-system-in-romania", "title_key"),
        ("implementation-bim-roadmap-greece-including-pilot-projects",
         "tsi-2023-implementation-of-the-bim-roadmap-in-greece-including-pilot-projects", "title_key"),
    }
    assert all(m["score"] == 1.0 for m in report["merged_into_thin"])
    assert [p["project_id"] for p in report["new_projects"]] == ["sustainable-development-strategy-malta-2050"]
    assert report["duplicates_merged"] == [{
        "detail_id": "modernization-national-data-information-infrastructure-statistics-poland",
        "thin_id": "srsp-2019-modernization-of-the-national-data-information-infrastructure-statistics-poland",
        "score": 1.0}]


def test_non_project_pages_are_skipped(pages):
    disc = ProjectDiscoverer(sitemap_excluded_areas=[])
    disc._fetcher = detail_fetcher(**{f"{BASE}/our-projects-0/italy_en": b"<html><main><h1>Italy</h1></main></html>"},
                                   )
    disc._fetcher.default = lambda url: b"<html><main><h1>Other</h1></main></html>"
    out = disc.run(pages=pages)
    assert "tsi-2025-flagship-pace-public-administration-cooperation-exchange" not in by_id(out["refs"])
    assert FLAGSHIP in out["report"]["sitemap"]["not_project_pages"]


def test_sitemap_detail_failures(pages):
    class Flaky(FakeFetcher):
        def get(self, url, cache=True):
            if url == MALTA:
                self.calls.append(url)
                raise FetchError(url, "HTTP 503", 503)
            if url == BIM:
                self.calls.append(url)
                raise FetchError(url, "HTTP 404", 404)
            return super().get(url, cache)

    disc = ProjectDiscoverer()
    disc._fetcher = Flaky(pages={ROMANIA: fixture_bytes("detail_licensing_romania.html")})
    out = disc.run(pages=pages)
    ids = by_id(out["refs"])
    assert "sustainable-development-strategy-malta-2050" not in ids
    assert "tsi-2023-implementation-of-the-bim-roadmap-in-greece-including-pilot-projects" in ids  # reste thin
    # Echec temporaire : ni ecrit, ni de-liste au run suivant.
    assert out["deferred_ids"] == ["sustainable-development-strategy-malta-2050", MALTA]
    assert out["report"]["sitemap"]["failed"] == [BIM]
    assert out["report"]["sitemap"]["deferred"] == [MALTA]


def test_sitemap_details_are_fetched_once_per_process(pages):
    disc = ProjectDiscoverer()
    disc._fetcher = detail_fetcher()
    first = disc.run(pages=pages)
    second = disc.run(pages=pages)
    assert disc._fetcher.calls == [ROMANIA, BIM, MALTA]
    assert [r.project_id for r in first["refs"]] == [r.project_id for r in second["refs"]]


def test_cached_detail_page_supplies_year_for_matching_only():
    # Page detail connue par un domaine politique (ni annee ni programme) : la page en cache donne 2023,
    # la mention thin de 2021 (deux ans d'ecart) ne doit pas etre fusionnee ; le ref garde year=None.
    html = f'<main><a href="{BIM}">Implementation of the BIM roadmap in Greece, including pilot projects</a></main>'
    thin = RawPage(kind="factsheet", key="GR", url="https://x/greece_en", content=(
        '<details class="ecl-accordion__item"><summary>2021</summary><div class="ecl-accordion__content">'
        '<ul><li>Implementation of the BIM Roadmap in Greece, including pilot projects.</li></ul></div></details>'
    ).encode())
    disc = ProjectDiscoverer()
    disc._fetcher = FakeFetcher(cached_pages={BIM: fixture_bytes("detail_bim_greece.html")})
    out = disc.run(pages=[RawPage(kind="policy_area", key="competitiveness", url="https://x/comp_en",
                                  content=html.encode()), thin])
    refs = by_id(out["refs"])
    assert len(refs) == 2
    assert refs["implementation-bim-roadmap-greece-including-pilot-projects"].year is None
    disc2 = ProjectDiscoverer()
    disc2._fetcher = FakeFetcher()  # rien en cache : annee inconnue = compatible, fusion
    assert len(disc2.run(pages=[RawPage(kind="policy_area", key="competitiveness", url="https://x/comp_en",
                                        content=html.encode()), thin])["refs"]) == 1
