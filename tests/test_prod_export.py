import json
import re

from tsi_agent.prod_export import CORPUS, check_meta_types, export

PROJECT = """---
project_id: alpha
title: Alpha
programme: TSI
year: 2022
countries: [GR]
multi_country: false
policy_area: green-transition
results: achieved
detail_page: true
source_urls:
- https://example.eu/alpha
---

# Alpha

Intro of Alpha.

## Context

Some context.
"""

DELIVERABLE = """---
publication_id: alpha-report
title: Alpha report
date: 1 May 2024
projects:
- alpha
countries:
- GR
year: 2022
programme: TSI
policy_area: green-transition
---

# Alpha report

## Final Report

Source: https://example.eu/alpha-report.pdf

--- page 1 ---
# Executive summary
# # Share of # # firms
#
The reform reduced delays by half in two years.

--- page 2 ---
Recommendations follow for the ministry and its agencies.
"""

PROGRAMME = """---
doc_id: country-greece
title: TSI country factsheet - Greece
source_url: https://example.eu/greece
---

# TSI country factsheet - Greece

Greece has 120 reform projects.
"""


def write_corpus(root):
    for folder in ("projects", "programme", "deliverables"):
        (root / folder).mkdir(parents=True)
    (root / "projects" / "alpha.md").write_text(PROJECT, encoding="utf-8")
    (root / "projects" / "broken.md").write_text("no front matter", encoding="utf-8")
    (root / "programme" / "country-greece.md").write_text(PROGRAMME, encoding="utf-8")
    (root / "deliverables" / "alpha-report.md").write_text(DELIVERABLE, encoding="utf-8")


def test_export_writes_one_file_per_document_with_meta_and_manifest(tmp_path):
    write_corpus(tmp_path / "output")
    out = tmp_path / "build" / "files"
    (out).mkdir(parents=True)
    (out / "tsi-old.md").write_text("x", encoding="utf-8")
    manifest = export(tmp_path / "output", out)

    names = sorted(p.name for p in out.iterdir())
    assert names == ["tsi-deliverable-alpha-report.md", "tsi-deliverable-alpha-report.md.meta.json",
                     "tsi-programme-country-greece.md", "tsi-programme-country-greece.md.meta.json",
                     "tsi-project-alpha.md", "tsi-project-alpha.md.meta.json"]
    assert manifest["files_by_type"] == {"project": 1, "programme": 1, "deliverable": 1}
    assert manifest["chunks_by_type"] == {"project": 2, "programme": 1, "deliverable": 2}
    assert manifest["skipped"] == ["projects/broken.md"] and manifest["meta_type_problems"] == []
    assert json.loads((tmp_path / "build" / "manifest.json").read_text(encoding="utf-8")) == manifest

    project_meta = json.loads((out / "tsi-project-alpha.md.meta.json").read_text(encoding="utf-8"))
    assert project_meta == {
        "Topic": "TSI-BDG", "corpus": CORPUS, "doc_type": "project", "doc_id": "alpha", "title": "Alpha",
        "countries": ["GR"],
        "programme": "TSI", "year": 2022, "policy_area": "green-transition", "results": "achieved",
        "projects": ["alpha"], "multi_country": False, "detail_page": True,
        "source_url": "https://example.eu/alpha", "listing_titles": [], "chunks": 2}
    deliverable_meta = json.loads((out / "tsi-deliverable-alpha-report.md.meta.json").read_text(encoding="utf-8"))
    assert deliverable_meta["doc_type"] == "deliverable" and deliverable_meta["projects"] == ["alpha"]
    assert "source_url" not in deliverable_meta and "results" not in deliverable_meta
    programme_meta = json.loads((out / "tsi-programme-country-greece.md.meta.json").read_text(encoding="utf-8"))
    assert programme_meta["projects"] == [] and "year" not in programme_meta


def test_deliverable_sections_cite_document_page_and_projects(tmp_path):
    write_corpus(tmp_path / "output")
    out = tmp_path / "files"
    export(tmp_path / "output", out)
    text = (out / "tsi-deliverable-alpha-report.md").read_text(encoding="utf-8")
    headings = re.findall(r"^# .*$", text, re.MULTILINE)
    assert headings == ["# Alpha report — Final Report, p. 1 — TSI 2022 — Greece",
                        "# Alpha report — Final Report, p. 2 — TSI 2022 — Greece"]
    assert "Executive summary\nShare of # # firms\n\nThe reform" in text    # titres internes neutralises
    assert len(re.findall(r"^\s{0,3}#", text, re.MULTILINE)) == 2
    assert "Deliverable id: alpha-report · Projects: alpha · Source: https://example.eu/alpha-report.pdf" in text


def test_check_meta_types():
    assert check_meta_types([{"doc_id": "a", "year": 2022, "countries": ["GR"]}]) == []
    problems = check_meta_types([{"doc_id": "a", "year": "2022"}, {"doc_id": "b", "year": True},
                                 {"doc_id": "c", "foo": 1}])
    assert problems == ["a: year='2022' n'est pas int", "b: year=True n'est pas int", "c: cle inattendue foo"]


CSR_MD = """---
csr_id: csr-FR-2023-1
title: France 2023 CSR 1
countries: [FR]
year: 2023
number: '1'
policy_areas: [Healthcare]
assessment: Some Progress
subparts: [csr-FR-2023-1.1]
source_url: https://ec.europa.eu/economy_finance/country-specific-recommendations-database/
---

# France 2023 CSR 1

Improve the sustainability of the pension system.

## Sub-parts

- csr-FR-2023-1.1: Improve the sustainability of the pension system (Assessment: Some Progress)
"""


def test_export_includes_csr_files(tmp_path):
    output = tmp_path / "output"
    (output / "csr").mkdir(parents=True)
    (output / "csr" / "csr-FR-2023-1.md").write_text(CSR_MD, encoding="utf-8")
    out = tmp_path / "build" / "files"
    manifest = export(output, out)
    assert manifest["files_by_type"] == {"csr": 1} and manifest["meta_type_problems"] == []
    meta = json.loads((out / "tsi-csr-FR-2023-1.md.meta.json").read_text(encoding="utf-8"))
    assert meta == {"Topic": "TSI-BDG", "corpus": CORPUS, "doc_type": "csr", "doc_id": "csr-FR-2023-1",
                    "title": "France 2023 CSR 1", "countries": ["FR"], "year": 2023, "projects": [],
                    "source_url": "https://ec.europa.eu/economy_finance/country-specific-recommendations-database/",
                    "chunks": 2}
    text = (out / "tsi-csr-FR-2023-1.md").read_text(encoding="utf-8")
    assert text.startswith("# France 2023 CSR 1 — 2023 — France — Introduction\n")
    assert "Document id: csr-FR-2023-1" in text and "Detail page" not in text


def test_project_file_keeps_its_prod_name(tmp_path):
    root = tmp_path / "output"
    write_corpus(root)
    (root / "projects" / "project-x.md").write_text(PROJECT, encoding="utf-8")
    out = tmp_path / "build" / "files"
    export(root, out)
    assert (out / "tsi-project-project-x.md").exists()


def test_project_csrs_in_meta_only_when_present(tmp_path):
    output = tmp_path / "output"
    (output / "projects").mkdir(parents=True)
    (output / "projects" / "alpha.md").write_text(
        PROJECT.replace("results: achieved\n", "results: achieved\ncsrs: [csr-HR-2022-1.1]\n", 1), encoding="utf-8")
    (output / "projects" / "beta.md").write_text(PROJECT.replace("alpha", "beta").replace("Alpha", "Beta"),
                                                 encoding="utf-8")
    out = tmp_path / "build" / "files"
    manifest = export(output, out)
    assert manifest["meta_type_problems"] == []
    alpha = json.loads((out / "tsi-project-alpha.md.meta.json").read_text(encoding="utf-8"))
    beta = json.loads((out / "tsi-project-beta.md.meta.json").read_text(encoding="utf-8"))
    assert alpha["csrs"] == ["csr-HR-2022-1.1"] and "csrs" not in beta
