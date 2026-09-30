from collections import Counter

from conftest import fixture_bytes
from tsi_agent.parsers import main_text
from tsi_agent.parsers.detail import parse_detail
from tsi_agent.parsers.factsheet import parse_factsheet
from tsi_agent.parsers.policy_area import parse_policy_area
from tsi_agent.parsers.spotlight import parse_spotlight
from tsi_agent.parsers.srsp_pdf import parse_srsp_pdf

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
