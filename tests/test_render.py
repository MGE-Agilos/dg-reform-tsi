from tsi_agent.models import ProgrammeDoc, ProjectContent, ProjectRef
from tsi_agent.render import (NOT_REPORTED, detail_body, front_matter, parse_front_matter, programme_doc_text,
                              render, thin_body)

DETAIL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/health-and-long-term-care/hems_en"


def detail_ref():
    return ProjectRef(project_id="hems", title="HEMS in Croatia", programme="TSI", year=2022, countries=["HR"],
                      detail_url=DETAIL, policy_area="health-and-long-term-care", spotlight=True,
                      listing_urls=["https://x/croatia_en", "https://x/spotlight_en"])


def test_front_matter_roundtrip_and_field_order():
    fm = front_matter(detail_ref(), content_hash="sha256:1", first_seen="2026-09-30", last_updated="2026-09-30",
                      run_id="r1", model="claude-cli/sonnet")
    text = render(fm, "# body")
    parsed, body = parse_front_matter(text)
    assert parsed == fm
    assert list(parsed) == ["project_id", "title", "programme", "year", "countries", "multi_country", "policy_area", "spotlight", "highlighted", "detail_page", "listed", "aliases", "source_urls", "content_hash", "first_seen", "last_updated", "extraction"]
    assert parsed["source_urls"][0] == DETAIL and parsed["detail_page"] is True
    assert body == "# body\n"


def test_parse_front_matter_is_anchored_on_lines():
    fm = {"project_id": "p", "title": "line one\n---\nline three"}
    text = render(fm, "# body\n\n---\n\nafter rule")
    parsed, body = parse_front_matter(text)
    assert parsed == fm
    assert body == "# body\n\n---\n\nafter rule\n"


def test_detail_body_fills_missing_sections():
    content = ProjectContent(summary="S.", context="C.", support_delivered="", results_achieved="R.")
    body = detail_body(detail_ref(), content)
    assert body.startswith("# HEMS in Croatia\n\n## Summary\n\nS.")
    assert f"## Support delivered\n\n{NOT_REPORTED}" in body
    assert body.endswith(f"## Sources\n\n- {DETAIL}\n- https://x/croatia_en\n- https://x/spotlight_en")


def test_thin_bodies():
    tsi = ProjectRef(project_id="tsi-2025-x", title="X", programme="TSI", year=2025, countries=["FR", "DE"],
                     listing_urls=["https://x/france_en"])
    assert ("Project listed in the TSI country factsheets for France, Germany under TSI 2025 (multi-country). "
            "No detail page is published.") in thin_body(tsi)
    srsp = ProjectRef(project_id="srsp-2019-y", title="Y", programme="SRSP", year=2019, countries=["AT"],
                      listing_urls=["https://x/srsp.pdf"])
    assert "SRSP list of approved technical support requests (Austria, 2019)" in thin_body(srsp)


def test_programme_doc_text():
    doc = ProgrammeDoc(doc_id="country-croatia", title="Croatia", url="https://x/croatia_en", text="138 projects")
    fm, body = parse_front_matter(programme_doc_text(doc, "r1", "2026-09-30"))
    assert fm["doc_id"] == "country-croatia" and fm["content_hash"] == doc.content_hash
    assert "138 projects" in body
