from tsi_agent.countries import iso_from_name, iso_from_slug, name_from_iso, slug_from_iso
from tsi_agent.ids import clean_text, sha256_text, slug_from_url, slugify, title_key
from tsi_agent.urls import (absolutize, is_canonical_detail, is_detail_candidate, is_final_detail,
                            policy_area_from_url)

DETAIL = ("https://reforms-investments.ec.europa.eu/technical-support-instrument-0/"
          "health-and-long-term-care/establishment-helicopter-emergency-medical-services-croatia_en")


def test_slugify_strips_accents_pace_prefix_and_punctuation():
    assert slugify("PACE - Réforme  de l'État (EVADE)") == "reforme-de-l-etat-evade"
    assert slugify("PACE – Energy") == "energy"
    assert len(slugify("x" * 200)) == 80


def test_title_key_and_slug_from_url():
    assert title_key("TSI", 2025, "Automation of routine tasks (ARTJP)") == "tsi-2025-automation-of-routine-tasks-artjp"
    assert slug_from_url(DETAIL) == "establishment-helicopter-emergency-medical-services-croatia"


def test_clean_text_and_hash():
    assert clean_text("  a\n  b\t c ") == "a b c"
    assert sha256_text("a").startswith("sha256:") and sha256_text("a") != sha256_text("b")
    assert clean_text("\u200b\u200bPACE - \u200bX\ufeff") == "PACE - X"


def test_invisible_characters_do_not_split_a_project():
    # same project listed by two factsheets, one with zero-width spaces before "PACE"
    a = title_key("TSI", 2026, "\u200b\u200bPACE - Cross-Regional Wildfire Prevention Exchange\u200b")
    assert a == title_key("TSI", 2026, "PACE - Cross-Regional Wildfire Prevention Exchange")


def test_countries_lookup():
    assert iso_from_slug("czechia") == "CZ"
    assert slug_from_iso("GR") == "greece"
    assert iso_from_name("Czech Republic") == "CZ"
    assert iso_from_name("The  Netherlands") == "NL"
    assert iso_from_name("Europe") is None
    assert name_from_iso("HR") == "Croatia"


def test_detail_url_rules():
    assert is_canonical_detail(DETAIL) and is_final_detail(DETAIL)
    assert policy_area_from_url(DETAIL) == "health-and-long-term-care"
    old = "https://reform-support.ec.europa.eu/what-we-do/health-and-long-term-care/healthier-online-gambling_en"
    assert is_detail_candidate(old) and not is_canonical_detail(old) and not is_final_detail(old)
    odd = absolutize("/technical-support-instrument-0/what-we-do/finance-and-access-finance/support-insolvency-croatia_en")
    assert is_detail_candidate(odd) and not is_canonical_detail(odd) and is_final_detail(odd)
    assert not is_detail_candidate(absolutize("/document/download/70c3_en?filename=TSI.pdf"))
    assert not is_detail_candidate(absolutize("/technical-support-instrument-0/green-transition_en"))
    assert not is_detail_candidate(absolutize("/technical-support-instrument-0/our-projects/tsi-country-factsheets/france_en"))
    assert not is_detail_candidate("https://commission.europa.eu/system/files/2021-02/tsi_2021_country_factsheet_france.pdf")
