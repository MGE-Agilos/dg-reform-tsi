from tsi_agent.models import ProgrammeDoc, ProjectContent, ProjectRecord, ProjectRef
from tsi_agent.render import (detail_body, front_matter, parse_front_matter, programme_doc_text, render,
                              replace_listing_tail, thin_body)

DETAIL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/health-and-long-term-care/hems_en"


def detail_ref():
    return ProjectRef(project_id="hems", title="HEMS in Croatia", programme="TSI", year=2022, countries=["HR"],
                      detail_url=DETAIL, policy_area="health-and-long-term-care", spotlight=True,
                      listing_urls=["https://x/croatia_en", "https://x/spotlight_en"])


def rec(ref, summary="S.", **kw):
    return ProjectRecord(ref=ref, listing_hash="h", content=ProjectContent(summary=summary), content_hash="sha256:1",
                         model="m", **kw)


def test_front_matter_roundtrip_and_field_order():
    fm = front_matter(detail_ref(), content_hash="sha256:1", first_seen="2026-09-30", last_updated="2026-09-30",
                      run_id="r1", model="claude-cli/sonnet", results="expected")
    text = render(fm, "# body")
    parsed, body = parse_front_matter(text)
    assert parsed == fm
    assert list(parsed) == ["project_id", "title", "listing_titles", "programme", "year", "countries", "multi_country", "policy_area", "results", "spotlight", "highlighted", "detail_page", "listed", "aliases", "source_urls", "content_hash", "first_seen", "last_updated", "extraction"]
    assert parsed["source_urls"][0] == DETAIL and parsed["detail_page"] is True
    assert parsed["results"] == "expected"
    thin = front_matter(detail_ref(), content_hash=None, first_seen="d", last_updated="d", run_id="r", model="none")
    assert thin["results"] is None
    assert body == "# body\n"


def test_parse_front_matter_is_anchored_on_lines():
    fm = {"project_id": "p", "title": "line one\n---\nline three"}
    text = render(fm, "# body\n\n---\n\nafter rule")
    parsed, body = parse_front_matter(text)
    assert parsed == fm
    assert body == "# body\n\n---\n\nafter rule\n"


def test_detail_body_keeps_official_sections_verbatim_after_labelled_summary():
    record = rec(detail_ref(), intro="The Commission supported HEMS.",
                 sections=[("Context", "Croatia has islands."), ("Expected results", "- a,\n- b.")],
                 results="expected", page_text="full text")
    body = detail_body(detail_ref(), record)
    assert body.startswith("# HEMS in Croatia\n\nThe Commission supported HEMS.\n\n## Summary (AI-generated)\n\nS.\n\n"
                           "## Context\n\nCroatia has islands.\n\n## Expected results\n\n- a,\n- b.\n\n## Sources")
    assert "full text" not in body and "## Page text" not in body
    assert body.endswith(f"## Sources\n\n- {DETAIL}\n- https://x/croatia_en\n- https://x/spotlight_en")


def test_detail_body_falls_back_to_the_full_page_text_without_sections():
    body = detail_body(detail_ref(), rec(detail_ref(), page_text="Title\nSome text\nMore text"))
    assert body.startswith("# HEMS in Croatia\n\n## Summary (AI-generated)\n\nS.\n\n"
                           "## Page text\n\nTitle\nSome text\nMore text\n\n## Sources")


def test_replace_listing_tail_keeps_official_sections():
    record = rec(detail_ref(), sections=[("Context", "C."), ("Results achieved", "R.")])
    body = detail_body(detail_ref(), record)
    new_ref = detail_ref().model_copy(update={"listing_urls": ["https://x/new"]})
    updated = replace_listing_tail(body, new_ref)
    assert "## Context\n\nC.\n\n## Results achieved\n\nR.\n\n## Sources\n\n" in updated
    assert updated.endswith("- https://x/new")


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


def test_detail_body_lists_distinct_listing_titles_before_sources():
    ref = ProjectRef(project_id="social-climate-plan", title="Support preparation social climate plans",
                     programme="TSI", year=2024, countries=["CZ", "DK"], detail_url=DETAIL,
                     listing_titles=["Social Climate Plan for Czechia", "Social Climate Plan for Denmark",
                                     "social climate plan for czechia"])
    body = detail_body(ref, rec(ref))
    assert ("## Listed in country factsheets as\n\n- Social Climate Plan for Czechia\n"
            "- Social Climate Plan for Denmark\n\n## Sources") in body


def test_detail_body_omits_listed_section_when_only_title_case_insensitively():
    ref = ProjectRef(project_id="hems", title="HEMS in Croatia", programme="TSI", year=2022, countries=["HR"],
                     detail_url=DETAIL, listing_titles=["hems in croatia"])
    body = detail_body(ref, rec(ref))
    assert "## Listed in country factsheets as" not in body


def test_listing_hash_changes_with_listing_titles():
    base = dict(project_id="p", title="T", programme="TSI", year=2025, countries=["FR"])
    a = ProjectRef(listing_titles=["x"], **base)
    b = ProjectRef(listing_titles=["y"], **base)
    assert a.listing_hash() != b.listing_hash()


def test_replace_listing_tail():
    body = "# T\n\n## Summary\n\nS.\n\n## Sources\n\n- old"
    new_ref = ProjectRef(project_id="p", title="T", programme="TSI", year=2025, countries=["FR"],
                         listing_urls=["https://x/new"])
    updated = replace_listing_tail(body, new_ref)
    assert updated.rstrip() == "# T\n\n## Summary\n\nS.\n\n## Sources\n\n- https://x/new".rstrip()

    no_tail = "# T\n\n## Summary\n\nS."
    appended = replace_listing_tail(no_tail, new_ref)
    assert appended.rstrip() == "# T\n\n## Summary\n\nS.\n\n## Sources\n\n- https://x/new".rstrip()


def test_listed_as_ignores_leading_article_and_punctuation():
    ref = ProjectRef(project_id="era", title="The Energy Regulation Academy",
                     listing_titles=["Energy Regulation Academy", "Energy regulation academy."], listing_urls=["u"])
    assert "Listed in country factsheets as" not in detail_body(ref, rec(ref))
