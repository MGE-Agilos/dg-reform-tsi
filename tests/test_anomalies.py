import csv
import json

from tsi_agent.anomalies import Anomaly, detect, load_projects, load_publications, project_anomalies, run


def _project(pid, **kw):
    base = {"project_id": pid, "title": pid.replace("-", " ").title(), "programme": "TSI", "year": 2022,
            "countries": ["GR"], "detail_page": True, "results": "achieved", "deliverables": 1,
            "source_urls": [f"https://example.eu/{pid}"], "first_seen": "2026-09-30"}
    base.update(kw)
    return base


def _types(rows):
    return sorted((r.type, r.severity) for r in rows)


def test_tsi_not_published_severity_by_age():
    old = project_anomalies(_project("a", detail_page=False, year=2024), current_year=2026)
    last = project_anomalies(_project("b", detail_page=False, year=2025), current_year=2026)
    new = project_anomalies(_project("c", detail_page=False, year=2026), current_year=2026)
    unknown = project_anomalies(_project("d", detail_page=False, year=None), current_year=2026)
    assert _types(old) == [("tsi_not_published", "high")]
    assert _types(last) == [("tsi_not_published", "medium")]
    assert _types(new) == [("tsi_not_published", "low")]
    assert _types(unknown) == [("tsi_not_published", "medium")]


def test_srsp_without_page_is_info_only():
    rows = project_anomalies(_project("s", programme="SRSP", detail_page=False, results=None, deliverables=None),
                             current_year=2026)
    assert _types(rows) == [("srsp_title_only", "info")]


def test_published_without_deliverable():
    achieved = project_anomalies(_project("a", deliverables=None), current_year=2026)
    expected = project_anomalies(_project("b", deliverables=None, results="expected"), current_year=2026)
    assert _types(achieved) == [("published_no_deliverable", "medium")]
    assert _types(expected) == [("published_no_deliverable", "low")]


def test_missing_results():
    rows = project_anomalies(_project("a", results=None), current_year=2026)
    assert _types(rows) == [("missing_results", "low")]


def test_clean_project_has_no_anomaly():
    assert project_anomalies(_project("a"), current_year=2026) == []


def test_row_fields():
    (row,) = project_anomalies(_project("a", detail_page=False, countries=["HR", "SI"]), current_year=2026)
    assert row == Anomaly(project_id="a", title="A", programme="TSI", year=2022, countries="HR,SI",
                          type="tsi_not_published", severity="high", detail="no detail page (first seen 2026-09-30)",
                          url="https://example.eu/a")


def test_orphan_deliverables_use_ids_and_aliases():
    projects = [_project("a", aliases=["old-a"])]
    pubs = [{"publication_id": "p1", "title": "P1", "url": "u1", "project_ids": ["a"]},
            {"publication_id": "p2", "title": "P2", "url": "u2", "project_ids": ["old-a"]},
            {"publication_id": "p3", "title": "P3", "url": "u3", "project_ids": ["ghost"]},
            {"publication_id": "p4", "title": "P4", "url": "u4", "project_ids": []}]
    rows = [r for r in detect(projects, pubs, 2026) if r.type == "orphan_deliverable"]
    assert [r.detail for r in rows] == ["p3: references unknown ghost", "p4: references no project"]
    assert all(r.project_id == "" and r.severity == "medium" for r in rows)


def test_possible_duplicates_need_same_programme_and_country():
    projects = [_project("x1", title="Support to the reform of the Greek tax administration"),
                _project("x2", title="Support to the reform of the tax administration of Greece", year=2023),
                _project("x3", title="Support to the reform of the tax administration", countries=["HR"]),
                _project("x4", title="Support to the reform of the Greek tax administration", programme="SRSP")]
    rows = [r for r in detect(projects, [], 2026) if r.type == "possible_duplicate"]
    assert [(r.project_id, r.detail.split(" (")[0]) for r in rows] == [("x1", "similar to x2")]


def test_detect_sorts_by_severity_then_type():
    projects = [_project("z", results=None), _project("y", detail_page=False, year=2021),
                _project("w", programme="SRSP", detail_page=False)]
    assert [(r.severity, r.type) for r in detect(projects, [], 2026)] == [
        ("high", "tsi_not_published"), ("low", "missing_results"), ("info", "srsp_title_only")]
