from conftest import FakeFetcher, fixture_bytes
from tsi_agent.components.discoverer import ProjectDiscoverer, merge_refs
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


def test_distinct_projects_stay_distinct():
    a = ProjectRef(project_id="tsi-2025-a", title="A", programme="TSI", year=2025, countries=["FR"])
    b = ProjectRef(project_id="tsi-2024-a", title="A", programme="TSI", year=2024, countries=["FR"])
    assert len(merge_refs([a, b])) == 2


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
    assert out["scope"] == {"countries": ["HR"], "srsp": False, "all_countries": False}
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
    assert out["deferred_ids"] == ["busy", "tsi-2025-busy-project"]
