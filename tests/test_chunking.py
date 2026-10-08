import pytest

from conftest import FIXTURES
from tsi_agent.chunking import (CHUNKER_VERSION, INTRO, SUMMARY, chunk_file, context_header, split_sections,
                                split_words)


def test_deliverables_section_is_ai_generated():
    text = read("vec_detail_sections.md").replace(
        "## Sources", "## Deliverables (AI-generated summary)\n\n### Report (1 May 2025)\n\nDelivered X.\n\n## Sources")
    docs = chunk_file("projects/x.md", text)
    deliv = [d for d in docs if d.meta["section"] == "Deliverables (AI-generated summary)"]
    assert len(deliv) == 1 and deliv[0].meta["ai_generated"] is True and "Delivered X." in deliv[0].content
    assert CHUNKER_VERSION == 3


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_split_sections_drops_title_sources_and_listed():
    body = ("# Title\n\nIntro text.\n\n## Summary (AI-generated)\n\nSum.\n\n## Context\n\nCtx.\n\n"
            "## Listed in country factsheets as\n\n- X\n\n## Sources\n\n- https://x\n")
    assert split_sections(body) == [(INTRO, "Intro text."), (SUMMARY, "Sum."), ("Context", "Ctx.")]


def test_split_sections_skips_empty_intro():
    assert split_sections("# T\n\n## Context\n\nCtx.\n") == [("Context", "Ctx.")]


def test_split_words_keeps_short_text_verbatim():
    text = "Line one.\nLine two."
    assert split_words(text) == [text]


def test_split_words_windows_with_overlap_and_sentence_cut():
    words = [f"w{i}" + ("." if i % 20 == 19 else "") for i in range(620)]
    chunks = split_words(" ".join(words))
    assert len(chunks) == 3
    first = chunks[0].split()
    assert len(first) <= 250 and first[-1].endswith(".")
    assert chunks[1].split()[0] == first[-40]          # recouvrement de 40 mots
    assert chunks[-1].split()[-1] == "w619."
    assert all(len(c.split()) <= 250 for c in chunks)


def test_context_header():
    fm = {"title": "HEMS", "programme": "TSI", "year": 2022, "countries": ["HR", "SI"]}
    assert context_header(fm) == "HEMS — TSI 2022 — Croatia, Slovenia"
    assert context_header({"title": "TSI country factsheet - Croatia"}) == "TSI country factsheet - Croatia"


def test_detail_file_with_sections():
    docs = chunk_file("projects/energy-regulation-academy.md", read("vec_detail_sections.md"))
    sections = [d.meta["section"] for d in docs]
    assert sections == [INTRO, SUMMARY, "Context", "Support delivered", "Expected results"]
    assert [d.id for d in docs] == [f"projects/energy-regulation-academy#{i}" for i in range(5)]
    first = docs[0].meta
    assert first["doc_type"] == "project" and first["detail_page"] is True and first["programme"] == "TSI"
    assert first["year"] == 2026 and len(first["countries"]) == 27 and first["multi_country"] is True
    assert first["source_file"] == "projects/energy-regulation-academy.md"
    assert first["source_url"].endswith("/green-transition/energy-regulation-academy_en")
    assert first["context_header"].startswith("The Energy Regulation Academy — TSI 2026 — Austria")
    assert [d.meta["ai_generated"] for d in docs] == [False, True, False, False, False]
    assert all("https://" not in d.content for d in docs)
    assert docs[0].content.startswith("The Energy Regulation Academy is a capacity-building initiative")


def test_detail_file_with_page_text():
    sections = [d.meta["section"] for d in chunk_file("projects/x.md", read("vec_detail_page_text.md"))]
    assert "Page text" in sections and SUMMARY in sections


def test_long_section_is_split():
    docs = chunk_file("projects/x.md", read("vec_detail_long_section.md"))
    support = [d for d in docs if d.meta["section"].lower() == "support delivered"]
    assert len(support) == 2
    assert [d.meta["chunk_index"] for d in docs] == list(range(len(docs)))


def test_thin_file_is_one_chunk():
    docs = chunk_file("projects/thin.md", read("vec_thin.md"))
    assert len(docs) == 1
    meta = docs[0].meta
    assert meta["detail_page"] is False and meta["programme"] == "SRSP" and meta["countries"] == ["GR"]
    assert "No detail page is published." in docs[0].content
    assert meta["source_url"].endswith(".pdf")


def test_programme_file():
    docs = chunk_file("programme/country-croatia.md", read("vec_programme.md"))
    assert docs and all(d.meta["doc_type"] == "programme" for d in docs)
    assert docs[0].meta["doc_id"] == "country-croatia" and docs[0].meta["detail_page"] is None
    assert docs[0].meta["source_url"].endswith("/croatia_en")


def test_crlf_file_is_accepted():
    text = read("vec_thin.md").replace("\n", "\r\n")
    assert len(chunk_file("projects/thin.md", text)) == 1


@pytest.mark.parametrize("text", ["no front matter", "---\n- a list\n---\n\nbody", "---\ntitle: x\n---\n\nbody"])
def test_invalid_files_raise_value_error(text):
    with pytest.raises(ValueError):
        chunk_file("projects/bad.md", text)


def test_chunker_version_is_an_int():
    assert isinstance(CHUNKER_VERSION, int)


def test_chunk_ids_are_scoped_by_file_path_not_doc_id():
    """Deux fichiers peuvent partager le meme project_id (front matter duplique) sans faire collision."""
    text = read("vec_thin.md")
    docs_a = chunk_file("projects/a.md", text)
    docs_b = chunk_file("projects/b.md", text)
    assert docs_a[0].id == "projects/a#0" and docs_b[0].id == "projects/b#0"
    assert docs_a[0].meta["doc_id"] == docs_b[0].meta["doc_id"]


def test_deliverable_is_chunked_per_page_with_project_meta():
    docs = chunk_file("deliverables/greece.md", read("vec_deliverable.md"))
    assert [(d.meta["section"], d.meta["page"]) for d in docs] == [
        ("Project public brief", 1), ("Project public brief", 2), ("Project public brief", 3),
        ("Factsheet", 1), ("Factsheet", 2), ("Factsheet", 3)]  # page 4 vide ignoree
    assert [d.id for d in docs] == [f"deliverables/greece#{i}" for i in range(6)]
    assert len({d.id for d in docs}) == 6 and [d.meta["chunk_index"] for d in docs] == list(range(6))
    first, factsheet = docs[0], docs[5]
    assert "Financing sustainability in Greece" in first.content and "--- page" not in first.content
    assert "FACTSHEET" in factsheet.content
    m = first.meta
    assert m["doc_type"] == "deliverable" and m["doc_id"] == "financing-sustainability-greece-public-policy-instruments"
    assert m["title"] == "Financing sustainability in Greece with public policy instruments"
    assert m["projects"] == ["financing-sustainability-greece-public-policy-instruments"]
    assert (m["countries"], m["year"], m["programme"]) == (["GR"], 2022, "TSI")
    assert m["policy_area"] == "financial-sector-and-access-finance" and m["detail_page"] is None
    assert m["ai_generated"] is False and m["source_file"] == "deliverables/greece.md"
    assert m["source_url"].startswith("https://reforms-investments.ec.europa.eu/document/download/845cc088")
    assert factsheet.meta["source_url"].startswith("https://reforms-investments.ec.europa.eu/document/download/8814d1dc")
    assert m["context_header"] == ("Financing sustainability in Greece with public policy instruments — "
                                   "Project public brief, p. 1 — TSI 2022 — Greece")


def test_deliverable_long_page_is_windowed_and_word_document_has_no_page():
    long_page = " ".join(f"w{i}" + ("." if i % 20 == 19 else "") for i in range(620))
    text = ("---\npublication_id: p\ntitle: T\nprojects: [a]\n---\n\n# T\n\n## Report\n\nSource: https://x/r\n\n"
            f"--- page 7 ---\n{long_page}\n\n## Word file\n\nSource: https://x/w.docx\n\nSome plain words here ok.\n")
    docs = chunk_file("deliverables/p.md", text)
    assert [d.meta["page"] for d in docs] == [7, 7, 7, None]
    assert docs[3].meta["section"] == "Word file" and docs[3].meta["context_header"] == "T — Word file"
    assert docs[3].meta["countries"] is None and docs[3].meta["year"] is None


def test_deliverable_front_matter_without_publication_id_is_still_a_project():
    assert chunk_file("projects/x.md", read("vec_thin.md"))[0].meta["doc_type"] == "project"


def test_chunk_file_window_is_parametrizable_without_changing_defaults():
    text = read("vec_detail_long_section.md")
    default = chunk_file("projects/x.md", text)
    assert [d.content for d in chunk_file("projects/x.md", text, threshold=300, window=250, overlap=40)] == \
        [d.content for d in default]
    small = chunk_file("projects/x.md", text, threshold=200, window=150, overlap=24)
    large = chunk_file("projects/x.md", text, threshold=450, window=400, overlap=64)
    assert len(small) > len(default) > len(large)
    assert max(len(d.content.split()) for d in small) <= 150
    assert all(d.id == f"projects/x#{i}" for i, d in enumerate(small))
    deliv = read("vec_deliverable.md")
    assert len(chunk_file("deliverables/g.md", deliv, threshold=60, window=50, overlap=8)) > \
        len(chunk_file("deliverables/g.md", deliv))
