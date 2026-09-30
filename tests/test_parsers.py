from collections import Counter

from conftest import fixture_bytes
from tsi_agent.parsers import main_text
from tsi_agent.parsers.detail import parse_detail
from tsi_agent.parsers.factsheet import parse_factsheet
from tsi_agent.parsers.policy_area import parse_policy_area
from tsi_agent.parsers.spotlight import parse_spotlight
from tsi_agent.parsers.srsp_pdf import heading_kind, parse_srsp_pdf

HR_URL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/our-projects/tsi-country-factsheets/croatia_en"


def test_factsheet_france_lists_every_year_with_single_and_multi_country():
    parsed = parse_factsheet(fixture_bytes("factsheet_france.html"), "https://x/france_en", "FR")
    listed = [r for r in parsed.refs if not r.highlighted]
    assert len(listed) == 66
    assert Counter(r.year for r in listed) == {2021: 4, 2022: 11, 2023: 13, 2024: 10, 2025: 19, 2026: 9}
    assert sum(r.detail_page for r in listed) == 37
    assert sum(r.multi_country for r in listed) == 38
    first = listed[0]
    assert first.title == "PACE - European Victim Architecture Digital Evaluation (EVADE)"
    assert first.project_id == "tsi-2026-european-victim-architecture-digital-evaluation-evade"
    assert (first.programme, first.countries, first.multi_country, first.detail_url) == ("TSI", ["FR"], False, None)
    assert parsed.announced[2026] == 10 and parsed.parsed_counts[2026] == 9


def test_factsheet_first_listed_ref_carries_its_listing_title():
    parsed = parse_factsheet(fixture_bytes("factsheet_france.html"), "https://x/france_en", "FR")
    listed = [r for r in parsed.refs if not r.highlighted]
    assert listed[0].listing_titles == ["PACE - European Victim Architecture Digital Evaluation (EVADE)"]


def test_factsheet_detail_refs_carry_their_thin_id_as_alias():
    parsed = parse_factsheet(fixture_bytes("factsheet_france.html"), "https://x/france_en", "FR")
    ref = next(r for r in parsed.refs if r.detail_url and r.detail_url.endswith("/energy-regulation-academy_en"))
    assert ref.project_id == "energy-regulation-academy"
    assert ref.aliases == ["tsi-2026-energy-regulation-academy"]
    assert ref.multi_country is True


def test_factsheet_croatia_highlights_and_programme_text():
    parsed = parse_factsheet(fixture_bytes("factsheet_croatia.html"), HR_URL, "HR")
    assert len([r for r in parsed.refs if not r.highlighted]) == 77
    highlighted = [r for r in parsed.refs if r.highlighted]
    assert [r.project_id for r in highlighted] == [
        "support-improve-poverty-diagnostic-and-social-policies-monitoring-croatia",
        "supporting-croatias-demographic-revitalisation-through-stronger-governance",
    ]
    assert all(r.year is not None and r.countries == ["HR"] for r in highlighted)
    assert "138 Reform projects" in parsed.programme_text
    assert "Highlighted projects:" in parsed.programme_text
    odd = [r for r in parsed.refs if r.detail_url and "/what-we-do/" in r.detail_url]
    assert len(odd) == 1


def test_spotlight_reads_country_from_flag_file_name():
    refs = parse_spotlight(fixture_bytes("spotlight.html"), "https://x/spotlight_en")
    assert len(refs) == 22
    assert all(r.spotlight for r in refs)
    hems = next(r for r in refs if r.project_id == "establishment-helicopter-emergency-medical-services-croatia")
    assert hems.countries == ["HR"]
    barnahus = next(r for r in refs if r.project_id == "barnahus-slovenia-supporting-children-victims-violence")
    assert barnahus.countries == ["SI"]  # le alt du drapeau dit "Poland", le fichier dit Slovenia
    assert any(r.multi_country and r.countries == [] for r in refs)


def test_policy_area_links_and_text():
    refs, text = parse_policy_area(fixture_bytes("policy_area_green_transition.html"), "https://x/green-transition_en")
    assert len(refs) == 14
    assert any(r.project_id == "improving-cooperation-between-business-and-science-lithuania" for r in refs)
    assert "Climate action and emissions reduction" in text


def test_detail_page():
    parsed = parse_detail(fixture_bytes("detail_hems_croatia.html"))
    assert parsed.title == "Establishment of Helicopter Emergency Medical Services in Croatia"
    assert (parsed.programme, parsed.year) == ("TSI", 2022)
    assert "Four HEMS bases were established" in parsed.text
    assert "Context" in parsed.text.splitlines()


def test_detail_page_keeps_official_intro_and_sections():
    parsed = parse_detail(fixture_bytes("detail_hems_croatia.html"))
    assert parsed.intro.startswith("The Commission supported the establishment")
    assert "Technical Support Instrument" not in parsed.intro and "healthcare systems" not in parsed.intro
    assert [h for h, _ in parsed.sections] == ["Context", "Support Delivered", "Results achieved"]
    support = dict(parsed.sections)["Support Delivered"]
    assert support.startswith("The project delivered technical support")
    assert "\n\n- workshops,\n- stakeholder dialogues,\n" in support
    assert dict(parsed.sections)["Results achieved"].startswith("Four HEMS bases were established")
    assert parsed.results == "achieved"


def test_detail_page_with_expected_results_and_sub_headings():
    parsed = parse_detail(fixture_bytes("detail_expected_results.html"))
    assert parsed.title == "Resources hub for sustainable investing in health"
    assert [h for h, _ in parsed.sections] == ["Context", "Support to be delivered", "Expected results"]
    assert parsed.results == "expected"
    assert parsed.intro.startswith("The Commission supports the creation of a hub")
    assert "EU support. It will also provide" in parsed.intro  # texte du lien garde, URL retiree
    assert "https://" not in parsed.intro
    support = dict(parsed.sections)["Support to be delivered"]
    assert "\n\n### - Work stream 1: Making the case for public investments\n\nWS 1 aims" in support
    expected = dict(parsed.sections)["Expected results"]
    assert "\n\n- Greening of the Austrian Health Care System and contribution" in expected
    assert "CO2 ecological footprint" in expected
    assert all("You can read the documents" not in body for _, body in parsed.sections)
    assert all("A mapping exercise" not in body for _, body in parsed.sections)  # "Useful links" exclu


def test_markdown_conversion_of_blocks():
    from tsi_agent.parsers import blocks_markdown, soup_of
    html = """<main><div class="ecl"><h2>T</h2><p>Intro  <em>with</em>
      <a href="https://x">link</a>.</p><h3>Context</h3><p>Line one<br>line two</p><p> </p>
      <figure><img src="x.png"><figcaption>caption</figcaption></figure>
      <ol><li>first<ul><li>nested <strong>a</strong></li></ul></li><li>second</li></ol>
      <h5>Sub</h5><div><p>In a div</p></div><h3>Photos</h3><p>boilerplate</p></div></main>"""
    parsed = parse_detail(html)
    assert parsed.intro == "Intro with link."
    assert parsed.sections == [("Context", "Line one\nline two\n\n1. first\n  - nested a\n2. second\n\n### Sub\n\nIn a div")]
    assert parsed.results is None
    assert blocks_markdown(soup_of("<p>a</p><ul><li>b</li></ul>").contents) == "a\n\n- b"


def test_performance_page_is_currently_empty():
    text = main_text(fixture_bytes("tsi_performance.html"))
    assert "PPS_TSI" not in text
    assert len(text) < 300


def test_srsp_pdf():
    refs = parse_srsp_pdf(fixture_bytes("srsp_list.pdf"), "https://x/srsp.pdf")
    assert len(refs) == 871
    assert Counter(r.countries[0] for r in refs)["AT"] == 17
    assert all(r.programme == "SRSP" and r.detail_url is None for r in refs)
    titles = {r.title for r in refs}
    assert ("Development of the Austrian Open Source Intelligence (OSINT) capabilities to fight and prevent "
            "cybercrime, fraud and economic crime") in titles
    first = refs[0]
    assert (first.countries, first.year, first.title) == (["AT"], 2020, "National strategy for financial literacy")
    assert first.project_id == "srsp-2020-national-strategy-for-financial-literacy"


def test_factsheet_nested_list_is_not_a_separate_project():
    html = """
    <main>
    <details class="ecl-accordion__item">
      <summary>2025</summary>
      <div class="ecl-accordion__content">
        <ul>
          <li><a href="/technical-support-instrument-0/green-transition/main-one_en">Main Project One</a>
            <ul><li>Sub-note about scope</li></ul>
          </li>
          <li>Main Project Two</li>
        </ul>
      </div>
    </details>
    </main>
    """
    parsed = parse_factsheet(html, "https://x/country_en", "FR")
    refs = [r for r in parsed.refs if not r.highlighted]
    assert len(refs) == 2
    assert [r.title for r in refs] == ["Main Project One", "Main Project Two"]
    assert refs[0].detail_url.endswith("/main-one_en")


def test_factsheet_ignores_cards_outside_main():
    html = """
    <main>
    <details class="ecl-accordion__item">
      <summary>2024</summary>
      <div class="ecl-accordion__content">
        <ul><li>Some Project</li></ul>
      </div>
    </details>
    </main>
    <footer>
      <article class="ecl-card">
        <a href="/technical-support-instrument-0/green-transition/teaser_en">2024 - Teaser</a>
      </article>
    </footer>
    """
    parsed = parse_factsheet(html, "https://x/country_en", "FR")
    assert not any(r.highlighted for r in parsed.refs)


def test_srsp_heading_kind():
    assert heading_kind("Austria") == "AT"
    assert heading_kind("The Netherlands") == "NL"
    assert heading_kind("Iceland") == "unknown"
    assert heading_kind("Funding") is None
    assert heading_kind("Title of the request") is None
    assert heading_kind("1") is None
