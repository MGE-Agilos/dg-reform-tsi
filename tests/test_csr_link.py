import json

from haystack.dataclasses import ChatMessage

from tsi_agent.csr import build_csrs
from tsi_agent.csr_link import (SECTION_HEADING, Candidate, candidates_hash, index_parts, project_input,
                                select_candidates)


def row(country="Croatia", year="2022", nbr="1.1", text="Reform the public administration.", areas="Public administration",
        version="2023 - Semester Cycle (May)", assessment="Some Progress"):
    return {"Country": country, "Year": year, "CSR Nbr": nbr, "Version": version, "Text": text,
            "Policy Area(s)": areas, "Assessment": assessment}


ROWS = [
    row(nbr="1", text="Reform the public administration. Improve the business environment."),
    row(nbr="1.1", text="Reform the public administration."),
    row(nbr="1.2", text="Improve the business environment.", areas="Business environment"),
    row(year="2020", nbr="2.1", text="Strengthen the health system.", areas="Healthcare"),
    row(year="2024", nbr="1.1", text="Too recent for a 2024 project."),
    row(country="Slovenia", year="2023", nbr="3.1", text="Reform pensions.", areas="Pensions"),
]


def parts():
    return index_parts(build_csrs(ROWS, year_from=2016, year_to=2025))


def test_index_parts_by_country_and_year():
    idx = parts()
    assert sorted(idx) == [("HR", 2020), ("HR", 2022), ("HR", 2024), ("SI", 2023)]
    hr = idx[("HR", 2022)]
    assert [c.part_id for c in hr] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]
    assert hr[0] == Candidate(part_id="csr-HR-2022-1.1", csr_id="csr-HR-2022-1", country="HR", year=2022,
                              text="Reform the public administration.", policy_areas=["Public administration"],
                              assessment="Some Progress")


def test_window_is_three_years_before_the_project():
    fm = {"project_id": "p", "title": "Public administration reform", "year": 2024, "countries": ["HR", "SI"]}
    ids = [c.part_id for c in select_candidates(fm, "", parts())]
    assert ids == ["csr-HR-2022-1.1", "csr-HR-2022-1.2", "csr-SI-2023-3.1"]  # 2021-2023 ; ni 2020 ni 2024


def test_no_year_or_country_gives_no_candidates():
    assert select_candidates({"project_id": "p", "title": "x", "year": None, "countries": ["HR"]}, "", parts()) == []
    assert select_candidates({"project_id": "p", "title": "x", "year": 2024, "countries": []}, "", parts()) == []


def test_many_countries_keep_closest_parts_per_country(monkeypatch):
    import tsi_agent.csr_link as mod

    monkeypatch.setattr(mod, "MAX_PER_COUNTRY", 1)
    fm = {"project_id": "p", "title": "Business environment", "year": 2024, "countries": ["HR", "SI", "GR"]}
    ids = [c.part_id for c in select_candidates(fm, "improve the business environment", parts())]
    assert ids == ["csr-HR-2022-1.2", "csr-SI-2023-3.1"]


def test_candidates_hash_ignores_order():
    a, b = parts()[("HR", 2022)]
    assert candidates_hash([a, b]) == candidates_hash([b, a]) != candidates_hash([a])


def test_project_input_detail_and_title_only():
    body = ("# Title\n\nIntro text.\n\n## Summary (AI-generated)\n\nSummary.\n\n## Objectives\n\nGoals.\n\n"
            f"{SECTION_HEADING}\n\n- old link\n\n## Deliverables (AI-generated summary)\n\nDeliv.\n\n"
            "## Sources\n\n- https://x")
    text, basis = project_input({"title": "Title", "detail_page": True}, body)
    assert basis == "full_text"
    assert "Intro text." in text and "Goals." in text and "Summary." in text
    assert "old link" not in text and "https://x" not in text and "Deliv." not in text
    text, basis = project_input({"title": "Thin", "detail_page": False, "listing_titles": ["Thin", "Thin bis"]},
                                "# Thin\n\n## Sources\n\n- u")
    assert basis == "title_only" and text == "Title: Thin\nListed as: Thin; Thin bis"
