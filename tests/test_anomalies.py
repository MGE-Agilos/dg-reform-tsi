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


PROJECT_MD = """---
project_id: {pid}
title: {title}
programme: TSI
year: 2021
countries:
- GR
detail_page: false
source_urls:
- https://example.eu/{pid}
first_seen: '2026-09-30'
---

# {title}
"""


def _output_dir(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "state").mkdir()
    (out / "projects" / "alpha.md").write_text(PROJECT_MD.format(pid="alpha", title="Alpha é"), encoding="utf-8")
    (out / "projects" / "broken.md").write_text("no front matter", encoding="utf-8")
    (out / "state" / "deliverables.json").write_text(json.dumps({"publications": {
        "p1": {"publication_id": "p1", "title": "P1", "url": "u1", "project_ids": ["ghost"]}}}), encoding="utf-8")
    return out


def test_loaders(tmp_path):
    out = _output_dir(tmp_path)
    assert [p["project_id"] for p in load_projects(out)] == ["alpha"]
    assert [p["publication_id"] for p in load_publications(out)] == ["p1"]
    assert load_publications(tmp_path / "missing") == []


def test_run_writes_csv_and_markdown(tmp_path):
    out = _output_dir(tmp_path)
    reports = tmp_path / "reports"
    summary = run(out, reports, current_year=2026)
    assert summary == {"total": 2, "by_type": {"tsi_not_published": 1, "orphan_deliverable": 1},
                       "by_severity": {"high": 1, "medium": 1}}
    raw = (reports / "anomalies.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM : Excel lit l'UTF-8
    with (reports / "anomalies.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ["project_id", "title", "programme", "year", "countries", "type", "severity",
                             "detail", "url"]
    assert rows[0]["title"] == "Alpha é" and rows[0]["year"] == "2021"
    assert rows[1]["year"] == ""
    md = (reports / "anomalies.md").read_text(encoding="utf-8")
    assert "# Publication anomalies" in md
    assert "| tsi_not_published | high | 1 |" in md
    assert "## tsi_not_published (1)" in md
    assert "Alpha é" in md


def test_load_projects_skips_invalid_and_non_dict_front_matter(tmp_path):
    out = _output_dir(tmp_path)
    (out / "projects" / "badyaml.md").write_text("---\ntitle: [unclosed\n---\n\nbody\n", encoding="utf-8")
    (out / "projects" / "listfm.md").write_text("---\n- a\n- b\n---\n\nbody\n", encoding="utf-8")
    assert [p["project_id"] for p in load_projects(out)] == ["alpha"]


def test_load_projects_normalises_atypical_fields(tmp_path):
    out = _output_dir(tmp_path)
    (out / "projects" / "odd.md").write_text(
        "---\nproject_id: odd\ntitle: Odd\nprogramme: TSI\nyear: '2021'\ncountries: GR\n---\n\nbody\n",
        encoding="utf-8")
    odd = next(p for p in load_projects(out) if p["project_id"] == "odd")
    assert odd["year"] == 2021 and odd["countries"] == ["GR"]


def test_duplicates_one_row_for_shared_countries_and_no_self_pair():
    t = "Support to the reform of the Greek tax administration"
    two = [_project("d1", title=t, countries=["GR", "HR"]), _project("d2", title=t, countries=["GR", "HR"])]
    rows = [r for r in detect(two, [], 2026) if r.type == "possible_duplicate"]
    assert len(rows) == 1
    twice = [_project("d1", title=t, countries=["GR", "GR"])]
    assert [r for r in detect(twice, [], 2026) if r.type == "possible_duplicate"] == []


def test_csv_neutralises_formula_cells(tmp_path):
    out = _output_dir(tmp_path)
    (out / "projects" / "evil.md").write_text(
        "---\nproject_id: evil\ntitle: '=HYPERLINK(\"x\")'\nprogramme: TSI\nyear: 2021\ncountries:\n- GR\n"
        "detail_page: false\n---\n\nbody\n", encoding="utf-8")
    reports = tmp_path / "reports"
    run(out, reports, current_year=2026)
    with (reports / "anomalies.csv").open(encoding="utf-8-sig", newline="") as fh:
        titles = [r["title"] for r in csv.DictReader(fh)]
    assert "'=HYPERLINK(\"x\")" in titles


def test_no_csr_link_only_for_processed_published_tsi():
    projects = [_project("a"), _project("b"), _project("c", programme="SRSP"), _project("d")]
    links = {"a": [], "b": ["csr-GR-2021-1.1"], "c": []}  # d : pas encore traite
    rows = [r for r in detect(projects, [], 2026, csr_links=links) if r.type == "no_csr_link"]
    assert [(r.project_id, r.severity) for r in rows] == [("a", "info")]


def test_run_reports_no_csr_link_from_csr_links_dir(tmp_path):
    out = _output_dir(tmp_path)
    (out / "projects" / "gamma.md").write_text(
        PROJECT_MD.format(pid="gamma", title="Gamma").replace("detail_page: false", "detail_page: true\nresults: achieved\ndeliverables: 1"),
        encoding="utf-8")
    (out / "csr_links").mkdir()
    (out / "csr_links" / "gamma.json").write_text(
        json.dumps({"project_id": "gamma", "status": "ok", "links": []}), encoding="utf-8")
    (out / "csr_links" / "broken.json").write_text("{", encoding="utf-8")
    summary = run(out, tmp_path / "reports", current_year=2026)
    assert summary["by_type"]["no_csr_link"] == 1
