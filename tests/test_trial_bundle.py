import json
import re

from haystack import Document

from tsi_agent.trial_bundle import MULTI, PROGRAMME, build, bundle_key, section, write_bundles

PROJECT = """---
project_id: {pid}
title: {title}
programme: TSI
year: 2022
countries: {countries}
policy_area: green-transition
results: achieved
detail_page: true
source_urls:
- https://example.eu/{pid}
---

# {title}

Intro of {title}.

## Context

Some context.

### Sub heading

Detail text.

## Sources

- https://example.eu/{pid}
"""

PROGRAMME_FILE = """---
doc_id: country-greece
title: TSI country factsheet - Greece
source_url: https://example.eu/greece
---

# TSI country factsheet - Greece

Greece has 120 reform projects.
"""


def write_corpus(root):
    (root / "projects").mkdir(parents=True)
    (root / "programme").mkdir()
    (root / "projects" / "a.md").write_text(PROJECT.format(pid="a", title="Alpha", countries="[GR]"), encoding="utf-8")
    (root / "projects" / "b.md").write_text(PROJECT.format(pid="b", title="Beta", countries="[GR, IT]"),
                                            encoding="utf-8")
    (root / "projects" / "bad.md").write_text("no front matter", encoding="utf-8")
    (root / "programme" / "country-greece.md").write_text(PROGRAMME_FILE, encoding="utf-8")


def test_bundle_key():
    assert bundle_key({"doc_type": "project", "countries": ["GR"]}) == "GR"
    assert bundle_key({"doc_type": "project", "countries": ["GR", "IT"]}) == MULTI
    assert bundle_key({"doc_type": "project", "countries": []}) == MULTI
    assert bundle_key({"doc_type": "programme", "countries": None}) == PROGRAMME


def test_section_has_context_heading_facts_and_no_inner_headings():
    doc = Document(content="Text.\n\n## Inner\n\n# Top\nMore.", meta={
        "doc_type": "project", "doc_id": "a", "context_header": "Alpha — TSI 2022 — Greece", "section": "Context",
        "policy_area": "green-transition", "results": "achieved", "detail_page": True,
        "source_url": "https://example.eu/a", "ai_generated": True})
    text = section(doc)
    assert text.startswith("# Alpha — TSI 2022 — Greece — Context\n\n")
    assert re.findall(r"^#", text, re.MULTILINE) == ["#"]          # un seul titre : celui de la section
    assert "Inner" in text and "Top\nMore." in text
    assert "(AI-generated summary, not the official text.)" in text
    assert text.rstrip().endswith("Project id: a · Policy area: green-transition · Results: achieved · "
                                  "Source: https://example.eu/a")


def test_build_groups_by_country_and_writes_meta(tmp_path):
    write_corpus(tmp_path / "output")
    out = tmp_path / "build"
    summary = build(tmp_path / "output", out)
    names = sorted(p.name for p in out.iterdir())
    assert names == ["tsi-greece.md", "tsi-greece.md.meta.json", "tsi-multi-country.md",
                     "tsi-multi-country.md.meta.json", "tsi-programme.md", "tsi-programme.md.meta.json"]
    assert summary["files"] == 3 and summary["skipped"] == ["projects/bad.md"]
    greece = (out / "tsi-greece.md").read_text(encoding="utf-8")
    assert "# Alpha — TSI 2022 — Greece — Introduction" in greece and "Beta" not in greece
    assert "Sources" not in greece                                    # section ignoree par le chunker
    assert json.loads((out / "tsi-greece.md.meta.json").read_text(encoding="utf-8")) == {
        "bundle": "GR", "country": "Greece", "chunks": 2}
    assert json.loads((out / "tsi-multi-country.md.meta.json").read_text(encoding="utf-8"))["country"] == MULTI
    assert summary["chunks"] == 5


def test_write_bundles_splits_parts_and_clears_old_files(tmp_path):
    (tmp_path / "tsi-old.md").write_text("x", encoding="utf-8")
    sections = [f"# S{i}\n\n" + "w " * 50 for i in range(4)]
    files = write_bundles({"GR": sections}, tmp_path, max_bytes=250)
    assert [f.name for f in files] == ["tsi-greece-part1.md", "tsi-greece-part2.md"]
    assert not (tmp_path / "tsi-old.md").exists()
    assert all(f.stat().st_size <= 250 for f in files)
