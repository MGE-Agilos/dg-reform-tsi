import itertools

from conftest import FakeFetcher, fixture_bytes
from tsi_agent.components.discoverer import ProjectDiscoverer, merge_refs
from tsi_agent.http import FetchError
from tsi_agent.models import ProjectRef, RawPage

BASE = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"


def test_merge_by_url_and_by_title_key():
    fr = ProjectRef(project_id="tsi-2025-academy", title="Academy", programme="TSI", year=2025, countries=["FR"],
                    multi_country=True, listing_urls=["https://x/france_en"])
    de = ProjectRef(project_id="academy", title="Academy", programme="TSI", year=2025, countries=["DE"],
                    multi_country=True, detail_url=f"{BASE}/green-transition/academy_en",
                    listing_urls=["https://x/germany_en"], aliases=["tsi-2025-academy"])
    spot = ProjectRef(project_id="academy", title="The Academy", detail_url=f"{BASE}/green-transition/academy_en",
                      spotlight=True, listing_urls=["https://x/spotlight_en"])
    merged = merge_refs([fr, de, spot])
    assert len(merged) == 1
    m = merged[0]
    assert m.project_id == "academy" and m.detail_url.endswith("/academy_en")
    assert m.countries == ["FR", "DE"] and m.multi_country and m.spotlight
    assert m.aliases == ["tsi-2025-academy"]
    assert m.listing_urls == ["https://x/france_en", "https://x/germany_en", "https://x/spotlight_en"]


def test_merge_unions_listing_titles_in_order_of_first_appearance():
    a = ProjectRef(project_id="a", title="A for France", programme="TSI", year=2025,
                   detail_url=f"{BASE}/t/a_en", countries=["FR"], listing_titles=["A for France"])
    b = ProjectRef(project_id="a", title="A for Italy", programme="TSI", year=2025,
                   detail_url=f"{BASE}/t/a_en", countries=["IT"], listing_titles=["A for Italy"])
    merged = merge_refs([a, b])
    assert len(merged) == 1
    assert merged[0].listing_titles == ["A for France", "A for Italy"]


def test_distinct_projects_stay_distinct():
    a = ProjectRef(project_id="tsi-2025-a", title="A", programme="TSI", year=2025, countries=["FR"])
    b = ProjectRef(project_id="tsi-2024-a", title="A", programme="TSI", year=2024, countries=["FR"])
    assert len(merge_refs([a, b])) == 2


def test_merge_is_transitive_and_order_independent():
    ref1 = ProjectRef(project_id="tsi-2025-academy", title="Academy", programme="TSI", year=2025,
                      countries=["FR"])
    ref2 = ProjectRef(project_id="academy", title="Academy Programme", detail_url="https://x/t/academy_en",
                      countries=["IT"])
    ref3 = ProjectRef(project_id="academy", title="Academy", programme="TSI", year=2025,
                      detail_url="https://x/t/academy_en", countries=["DE"])
    for perm in itertools.permutations([ref1, ref2, ref3]):
        merged = merge_refs(list(perm))
        assert len(merged) == 1, [r.project_id for r in merged]
        m = merged[0]
        assert m.project_id == "academy"
        assert set(m.countries) == {"FR", "IT", "DE"}
        assert "tsi-2025-academy" in m.aliases


def test_two_detail_pages_with_same_title_key_stay_separate():
    a = ProjectRef(project_id="a", title="Busy", programme="TSI", year=2025, detail_url="https://x/t/a_en",
                   countries=["FR"])
    b = ProjectRef(project_id="b", title="Busy", programme="TSI", year=2025, detail_url="https://x/t/b_en",
                   countries=["DE"])
    thin = ProjectRef(project_id="tsi-2025-busy", title="Busy", programme="TSI", year=2025, countries=["IT"])
    merged = merge_refs([a, b, thin])
    # Clé ambiguë (2 URLs détail distinctes) : le thin ne rejoint plus arbitrairement l'un des deux
    # pays (ancien comportement dépendant de l'ordre) ; il reste un troisième groupe à part entière.
    assert len(merged) == 3
    assert sum(1 for r in merged if r.detail_url) == 2


def test_flagship_title_shared_by_several_country_pages():
    import itertools
    base = dict(programme="TSI", year=2022, title="Support to the renovation wave", multi_country=True)
    ee = ProjectRef(project_id="reno-estonia", detail_url="https://x/t/reno-estonia_en", countries=["EE"],
                    aliases=["tsi-2022-support-to-the-renovation-wave"], **base)
    ie = ProjectRef(project_id="reno-ireland", detail_url="https://x/t/reno-ireland_en", countries=["IE"],
                    aliases=["tsi-2022-support-to-the-renovation-wave"], **base)
    fr = ProjectRef(project_id="tsi-2022-support-to-the-renovation-wave", countries=["FR"], **base)
    de = ProjectRef(project_id="tsi-2022-support-to-the-renovation-wave", countries=["DE"], **base)
    results = set()
    for perm in itertools.permutations([ee, ie, fr, de]):
        groups = merge_refs(list(perm))
        results.add(tuple(sorted((g.project_id, tuple(sorted(g.countries)), tuple(g.aliases)) for g in groups)))
    assert results == {(
        ("reno-estonia", ("EE",), ()),
        ("reno-ireland", ("IE",), ()),
        ("tsi-2022-support-to-the-renovation-wave", ("DE", "FR"), ()),
    )}


def test_resolves_non_canonical_links_and_builds_programme_docs():
    hr_url = f"{BASE}/our-projects/tsi-country-factsheets/croatia_en"
    odd = f"{BASE}/what-we-do/finance-and-access-finance/support-improve-data-collection-insolvency-croatia_en"
    final = f"{BASE}/financial-sector-and-access-finance/support-improve-data-collection-insolvency-croatia_en"
    disc = ProjectDiscoverer()
    disc._fetcher = FakeFetcher(redirects={odd: final}, default=lambda url: b"<html></html>")
    pages = [
        RawPage(kind="factsheet", key="HR", url=hr_url, content=fixture_bytes("factsheet_croatia.html")),
        RawPage(kind="programme_doc", key="tsi-performance", url="https://x/perf_en",
                content=fixture_bytes("tsi_performance.html"), title="TSI performance"),
    ]
    out = disc.run(pages=pages)
    ids = {r.project_id for r in out["refs"]}
    assert "support-improve-data-collection-insolvency-croatia" in ids
    resolved = next(r for r in out["refs"] if r.project_id == "support-improve-data-collection-insolvency-croatia")
    assert resolved.policy_area == "financial-sector-and-access-finance"
    assert disc._fetcher.calls == [odd]
    assert [d.doc_id for d in out["programme_docs"]] == ["country-croatia"]  # performance vide => écartée
    assert out["scope"] == {"countries": ["HR"], "srsp": False, "all_countries": False,
                            "fetched": sorted([hr_url, "https://x/perf_en"])}
    assert out["suspect_countries"] == []


def test_broken_link_becomes_thin_and_empty_factsheet_is_suspect():
    html = f"""<details class="ecl-accordion__item"><summary>2025</summary><div class="ecl-accordion__content">
      <p>For 2025 the following <strong>1</strong> project:</p>
      <ul><li><a href="https://reform-support.ec.europa.eu/what-we-do/x/gone_en">Gone project</a></li></ul>
      </div></details>""".encode()
    disc = ProjectDiscoverer()
    disc._fetcher = FakeFetcher()
    out = disc.run(pages=[
        RawPage(kind="factsheet", key="FR", url="https://x/france_en", content=html),
        RawPage(kind="factsheet", key="DE", url="https://x/germany_en", content=b"<html><main></main></html>"),
    ])
    assert [r.project_id for r in out["refs"]] == ["tsi-2025-gone-project"]
    assert out["refs"][0].detail_url is None and out["refs"][0].aliases == []
    assert out["suspect_countries"] == ["DE"]


def test_transient_resolve_failure_is_deferred_not_thin():
    html = f"""<details class="ecl-accordion__item"><summary>2025</summary><div class="ecl-accordion__content">
      <ul><li><a href="https://reform-support.ec.europa.eu/what-we-do/x/busy_en">Busy project</a></li></ul>
      </div></details>""".encode()

    class RateLimited(FakeFetcher):
        def get(self, url):
            from tsi_agent.http import FetchError
            raise FetchError(url, "HTTP 429")

    disc = ProjectDiscoverer()
    disc._fetcher = RateLimited()
    out = disc.run(pages=[RawPage(kind="factsheet", key="FR", url="https://x/france_en", content=html)])
    assert out["refs"] == []
    assert out["deferred_ids"] == ["busy", "tsi-2025-busy-project",
                                   "https://reform-support.ec.europa.eu/what-we-do/x/busy_en"]


def test_unexpected_resolve_error_defers_instead_of_crashing():
    html = f"""<details class="ecl-accordion__item"><summary>2025</summary><div class="ecl-accordion__content">
      <ul><li><a href="https://reform-support.ec.europa.eu/what-we-do/x/busy_en">Busy project</a></li></ul>
      </div></details>""".encode()

    class Broken(FakeFetcher):
        def get(self, url):
            raise ValueError("boom")

    disc = ProjectDiscoverer()
    disc._fetcher = Broken()
    out = disc.run(pages=[RawPage(kind="factsheet", key="FR", url="https://x/france_en", content=html)])
    assert out["refs"] == []
    assert out["deferred_ids"] == ["busy", "tsi-2025-busy-project",
                                   "https://reform-support.ec.europa.eu/what-we-do/x/busy_en"]


def test_permanent_403_becomes_thin():
    html = f"""<details class="ecl-accordion__item"><summary>2025</summary><div class="ecl-accordion__content">
      <ul><li><a href="https://reform-support.ec.europa.eu/what-we-do/x/busy_en">Busy project</a></li></ul>
      </div></details>""".encode()

    class Forbidden(FakeFetcher):
        def get(self, url):
            raise FetchError(url, "HTTP 403", 403)

    disc = ProjectDiscoverer()
    disc._fetcher = Forbidden()
    out = disc.run(pages=[RawPage(kind="factsheet", key="FR", url="https://x/france_en", content=html)])
    refs = out["refs"]
    assert len(refs) == 1
    assert refs[0].project_id == "tsi-2025-busy-project"
    assert refs[0].detail_url is None
    assert out["deferred_ids"] == []
