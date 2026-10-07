import json

import pytest

from tsi_agent.impact.report import (ReportFormatError, clean_citations, list_reports, parse_report, read_report,
                                     render_report, valid_project_id, write_report)

REPORT = {
    "project_overview": "Supports hospital reform [1].",
    "positive_evidence": "- Arrears fell 40% [3][9].",
    "negative_evidence": "Insufficient data on quality [2, 7].",
    "impact_by_level": {"country": "Lower arrears [3].", "europe": "Cited in the Semester [4].", "world": "None found."},
    "objectives": [{"objective": "Reduce arrears", "verdict": "achieved", "rationale": "See [3] | table"},
                   {"objective": "Improve quality", "verdict": "bogus", "rationale": "No data."}],
    "overall_verdict": "Positive but partly attributable.",
    "confidence": "medium",
}
SOURCES = [{"ref": 1, "title": "Project page", "url": "https://p.eu", "section": "Objectives"},
           {"ref": 2, "title": "Report", "url": "https://r.eu", "section": "Final report, p. 4"},
           {"ref": 3, "title": "News", "url": "https://n.eu", "kind": "web"},
           {"ref": 4, "title": "Semester", "url": "https://s.eu", "kind": "web"},
           {"ref": 5, "title": "Unused", "url": "https://u.eu", "kind": "web"}]
META = {"project_id": "hosp-hr", "title": "Hospital reform", "countries": ["HR"], "year": 2022, "programme": "TSI",
        "policy_area": "health-and-long-term-care"}
RUN = {"run_id": "r1", "model": "sonnet", "cost_usd": 0.5, "duration_s": 200.0, "generated": "2026-10-07"}


def test_parse_report_accepts_fences_and_rejects_garbage():
    assert parse_report("```json\n" + json.dumps(REPORT) + "\n```")["confidence"] == "medium"
    assert parse_report("Here it is: " + json.dumps(REPORT))["objectives"][0]["verdict"] == "achieved"
    with pytest.raises(ReportFormatError):
        parse_report("no json at all")


def test_clean_citations_removes_unknown_refs():
    text, removed = clean_citations("A [3][9] and [2, 7] and [12].", {2, 3})
    assert text == "A [3] and [2] and ." and removed == [7, 9, 12]


def test_render_report_structure_sources_and_front_matter():
    md = render_report(META, REPORT, SOURCES, RUN)
    assert md.startswith("---\n") and "overall_verdict: Positive but partly attributable." in md
    body = md.split("\n---\n", 1)[1]
    for heading in ("# Impact analysis — Hospital reform", "## 1. Project overview",
                    "## 2. Evidence of positive impact", "## 3. Evidence of negative impact or limitations",
                    "## 4. Impact by level", "### Country", "## 5. Assessment against objectives",
                    "## 6. Sources"):
        assert heading in body
    assert "[9]" not in body and "[7]" not in body
    assert "| Reduce arrears | achieved | See [3] \\| table |" in body
    assert "| Improve quality | insufficient evidence |" in body
    sources = body.split("## 6. Sources", 1)[1]
    assert "- [1] Project page — Objectives — https://p.eu" in sources
    assert "https://u.eu" not in sources  # source non citee
    from tsi_agent.render import parse_front_matter
    front = parse_front_matter(md)[0]
    assert front["sources"] == 4 and front["removed_citations"] == [7, 9]
    assert front["generated"] == "2026-10-07"


def test_write_list_read(tmp_path):
    write_report(tmp_path, "hosp-hr", render_report(META, REPORT, SOURCES, RUN))
    assert [r["project_id"] for r in list_reports(tmp_path)] == ["hosp-hr"]
    front, body = read_report(tmp_path, "hosp-hr")
    assert front["confidence"] == "medium" and body.startswith("# Impact analysis")
    assert read_report(tmp_path, "unknown") is None
    assert not valid_project_id("../etc") and valid_project_id("hosp-hr-2")
