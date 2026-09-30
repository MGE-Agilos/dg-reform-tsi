from tsi_agent.deliverables_state import DeliverableEntry, DeliverableStore
from tsi_agent.models import DeliverableContent
from tsi_agent.render import (DELIVERABLES_HEADING, deliverables_section, replace_deliverables_section,
                              with_deliverables_count)


def entry(pid="pub-a", date="17 October 2023", projects=("hems",), summary="Delivered a strategy."):
    return DeliverableEntry(
        publication_id=pid, url=f"https://x/publications-0/{pid}_en", title=f"Title {pid}", date=date,
        project_ids=list(projects), files_key="sha256:k",
        files=[{"document": "Final Report", "url": "https://x/d/1_en", "language": "en",
                "filename": "r.pdf", "pages": 12, "words": 4000}],
        full_text_path=f"deliverables/{pid}.md",
        content=DeliverableContent(summary=summary, key_outputs=["Strategy"], results=["Adopted"],
                                   figures=["40+ stakeholders"]),
        model="claude-cli/x", processed="2026-09-30")


def test_store_round_trip_and_for_project(tmp_path):
    s = DeliverableStore(tmp_path)
    s.put(entry("old", date="1 March 2022"))
    s.put(entry("new", date="5 May 2025"))
    s.put(entry("other", projects=("x",)))
    s.save()
    again = DeliverableStore(tmp_path)
    assert again.get("old").title == "Title old"
    assert [e.publication_id for e in again.for_project("hems")] == ["new", "old"]  # plus recente d'abord
    assert again.for_project("nope") == []


def test_section_text():
    text = deliverables_section([entry()])
    assert text.startswith(DELIVERABLES_HEADING + "\n\n### Title pub-a (17 October 2023)")
    assert "Files: [Final Report (EN, 12 p.)](https://x/d/1_en)" in text
    assert "Full text: deliverables/pub-a.md" in text
    assert "Key outputs:\n- Strategy" in text and "Results:\n- Adopted" in text
    assert "Key figures:\n- 40+ stakeholders" in text
    assert deliverables_section([]) is None


BODY = "# HEMS\n\nIntro.\n\n## Summary (AI-generated)\n\nS.\n\n## Context\n\nC.\n\n## Sources\n\n- https://x"


def test_replace_section_inserts_before_tail_then_replaces_then_removes():
    once = replace_deliverables_section(BODY, "## Deliverables (AI-generated summary)\n\nA")
    assert once.index("## Context") < once.index(DELIVERABLES_HEADING) < once.index("## Sources")
    twice = replace_deliverables_section(once, "## Deliverables (AI-generated summary)\n\nB")
    assert twice.count(DELIVERABLES_HEADING) == 1 and "\n\nB\n\n## Sources" in twice and "\n\nA\n" not in twice
    assert replace_deliverables_section(twice, None) == BODY
    assert replace_deliverables_section("# T\n\nx", "## Deliverables (AI-generated summary)\n\nA").endswith("\n\nA")


def test_deliverables_count_follows_results():
    fm = {"project_id": "p", "results": "achieved", "spotlight": False}
    assert list(with_deliverables_count(fm, 2)) == ["project_id", "results", "deliverables", "spotlight"]
    assert "deliverables" not in with_deliverables_count({**fm, "deliverables": 3}, 0)
