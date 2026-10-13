import json

import openpyxl

from tsi_agent.csr import COLUMNS
from tsi_agent.supabase_sync import TABLES, build_rows

PROJECT = """---
project_id: p1
title: Public administration reform
programme: TSI
year: 2024
countries:
- HR
policy_area: public-administration-and-governance
results: achieved
deliverables: 2
detail_page: true
source_urls:
- https://example.eu/p1
csrs:
- csr-HR-2022-1.1
---

# Public administration reform

Intro text.

## Summary (AI-generated)

The project reformed things.

## Sources

- https://example.eu/p1
"""

THIN = """---
project_id: p2
title: Thin project
programme: SRSP
year: 2019
countries:
- GR
detail_page: false
---

# Thin project
"""


def make_output(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "projects" / "p1.md").write_text(PROJECT, encoding="utf-8")
    (out / "projects" / "p2.md").write_text(THIN, encoding="utf-8")
    (out / "csr").mkdir()
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    for nbr, text in (("1", "Reform the administration. Cut red tape."), ("1.1", "Reform the administration."),
                      ("1.2", "Cut red tape.")):
        wb.active.append(["Croatia", "2022", nbr, "2023 - Semester Cycle (May)", text, "Public administration",
                          "-", "No", "Annual", "Some Progress"])
    wb.save(out / "csr" / "csr_full.xlsx")
    (out / "csr_links").mkdir()
    (out / "csr_links" / "p1.json").write_text(json.dumps({"project_id": "p1", "status": "ok", "basis": "full_text",
        "links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."},
                  {"csr_id": "csr-XX-2000-9.9", "level": "partial", "rationale": "unknown part"}]}), encoding="utf-8")
    (out / "csr_links" / "ghost.json").write_text(json.dumps({"project_id": "ghost", "status": "ok",
        "basis": "full_text", "links": [{"csr_id": "csr-HR-2022-1.2", "level": "partial", "rationale": "x"}]}),
        encoding="utf-8")
    return out


def test_build_rows_tables_and_projects(tmp_path):
    rows = build_rows(make_output(tmp_path), current_year=2026)
    assert list(rows) == list(TABLES) == ["projects", "csrs", "csr_parts", "project_csr_links", "anomalies"]
    p1, p2 = sorted(rows["projects"], key=lambda r: r["project_id"])
    assert p1 == {"project_id": "p1", "title": "Public administration reform", "programme": "TSI", "year": 2024,
                  "countries": ["HR"], "policy_area": "public-administration-and-governance", "results": "achieved",
                  "detail_page": True, "url": "https://example.eu/p1", "summary": "The project reformed things.",
                  "deliverables": 2, "csrs": ["csr-HR-2022-1.1"]}
    assert p2["summary"] is None and p2["csrs"] == [] and p2["deliverables"] == 0 and p2["url"] is None


def test_build_rows_csrs_parts_links_anomalies(tmp_path):
    rows = build_rows(make_output(tmp_path), current_year=2026)
    (csr,) = rows["csrs"]
    assert csr["csr_id"] == "csr-HR-2022-1" and csr["assessment"] == "Some Progress" and csr["assessment_year"] == 2023
    assert csr["policy_areas"] == ["Public administration"] and csr["title"] == "Croatia 2022 CSR 1"
    assert [p["part_id"] for p in rows["csr_parts"]] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]
    assert rows["csr_parts"][0]["csr_id"] == "csr-HR-2022-1"
    assert rows["project_csr_links"] == [{"project_id": "p1", "part_id": "csr-HR-2022-1.1", "level": "direct",
                                          "rationale": "R.", "basis": "full_text"}]
    types = {a["type"] for a in rows["anomalies"]}
    assert "srsp_title_only" in types
    assert all(set(a) == {"project_id", "title", "programme", "year", "countries", "type", "severity", "detail",
                          "url"} for a in rows["anomalies"])
    assert all(a["project_id"] is None or isinstance(a["project_id"], str) for a in rows["anomalies"])


def test_summary_falls_back_to_intro_and_is_capped(tmp_path):
    out = make_output(tmp_path)
    (out / "projects" / "p3.md").write_text(PROJECT.replace("p1", "p3").replace(
        "## Summary (AI-generated)\n\nThe project reformed things.\n\n", "").replace("Intro text.", "x" * 2000),
        encoding="utf-8")
    p3 = next(r for r in build_rows(out, current_year=2026)["projects"] if r["project_id"] == "p3")
    assert p3["summary"] == "x" * 1500
