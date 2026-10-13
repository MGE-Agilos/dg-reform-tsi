import openpyxl

from tsi_agent.chunking import chunk_file
from tsi_agent.csr import COLUMNS, build_csrs, read_rows, render_csr, write_csrs
from tsi_agent.render import parse_front_matter


def row(country="France", year="2023", nbr="1", version="2024 - Semester Cycle (June)", text="Text.",
        areas="Healthcare;Long-term care", assessment="Some Progress"):
    return {"Country": country, "Year": year, "CSR Nbr": nbr, "Version": version, "Text": text,
            "Policy Area(s)": areas, "EAR(s)": "-", "MIP": "No", "Annual/Multiannual": "Annual",
            "Assessment": assessment}


ROWS = [
    row(nbr="1", text="Improve the  sustainability of the pension system and of healthcare.",
        version="2024 - Semester Cycle (June)", assessment="Limited Progress"),
    row(nbr="1", text="Improve the  sustainability of the pension system and of healthcare.",
        version="2025 - Semester Cycle (June)", assessment="Some Progress"),
    row(nbr="1.2", text="and of healthcare.", areas="Healthcare", version="2025 - Semester Cycle (June)",
        assessment="Substantial Progress"),
    row(nbr="1.1", text="Improve the sustainability of the pension system", areas="Pension systems  and active ageing",
        version="2025 - Semester Cycle (June)", assessment="-"),
    row(country="Czech Republic", year="2019", nbr="2.1", text="Reduce red tape.", areas="-", assessment="-"),
    row(country="United Kingdom", year="2018", nbr="1"),
    row(year="2015", nbr="1"),
    row(year="2026", nbr="1"),
]


def test_build_groups_versions_and_subparts():
    csrs = {c.csr_id: c for c in build_csrs(ROWS)}
    assert sorted(csrs) == ["csr-CZ-2019-2", "csr-FR-2023-1"]
    fr = csrs["csr-FR-2023-1"]
    assert fr.text == "Improve the sustainability of the pension system and of healthcare."
    assert fr.assessments == [(2024, "Limited Progress"), (2025, "Some Progress")]
    assert fr.assessment == "Some Progress"
    assert [p.part_id for p in fr.subparts] == ["csr-FR-2023-1.1", "csr-FR-2023-1.2"]
    assert fr.subparts[0].policy_areas == ["Pension systems and active ageing"]
    assert fr.subparts[0].assessment is None
    assert fr.policy_areas == ["Healthcare", "Long-term care", "Pension systems and active ageing"]


def test_csr_without_full_row_uses_subpart_texts():
    cz = {c.csr_id: c for c in build_csrs(ROWS)}["csr-CZ-2019-2"]
    assert cz.text == "Reduce red tape." and cz.policy_areas == [] and cz.assessment is None


def test_year_window_is_configurable():
    assert {c.year for c in build_csrs(ROWS, year_from=2015, year_to=2026)} == {2015, 2019, 2023, 2026}


def test_render_front_matter_and_sections():
    fr = {c.csr_id: c for c in build_csrs(ROWS)}["csr-FR-2023-1"]
    fm, body = parse_front_matter(render_csr(fr))
    assert fm["csr_id"] == "csr-FR-2023-1" and fm["title"] == "France 2023 CSR 1"
    assert fm["countries"] == ["FR"] and fm["year"] == 2023 and fm["number"] == "1"
    assert fm["subparts"] == ["csr-FR-2023-1.1", "csr-FR-2023-1.2"]
    assert fm["assessment"] == "Some Progress" and fm["policy_areas"] == ["Healthcare", "Long-term care", "Pension systems and active ageing"]
    assert fm["source_url"].startswith("https://")
    assert body.startswith("# France 2023 CSR 1\n\nImprove the sustainability")
    assert "## Assessment\n\nLatest assessment (2025): Some Progress. History: 2024: Limited Progress; " \
           "2025: Some Progress." in body
    assert "- csr-FR-2023-1.2: and of healthcare. (Policy areas: Healthcare · Assessment: Substantial Progress)" in body
    assert "- csr-FR-2023-1.1: Improve the sustainability of the pension system (Policy areas: Pension systems " \
           "and active ageing)" in body


def test_write_replaces_old_files_and_chunks_as_csr(tmp_path):
    out = tmp_path / "csr"
    out.mkdir()
    (out / "csr-XX-2000-9.md").write_text("old", encoding="utf-8")
    (out / "csr_full.xlsx").write_bytes(b"keep")
    assert write_csrs(build_csrs(ROWS), out) == 2
    assert sorted(p.name for p in out.glob("*.md")) == ["csr-CZ-2019-2.md", "csr-FR-2023-1.md"]
    assert (out / "csr_full.xlsx").read_bytes() == b"keep"
    docs = chunk_file("csr/csr-FR-2023-1.md", (out / "csr-FR-2023-1.md").read_text(encoding="utf-8"))
    assert [d.meta["section"] for d in docs] == ["Introduction", "Assessment", "Sub-parts"]
    meta = docs[0].meta
    assert meta["doc_type"] == "csr" and meta["doc_id"] == "csr-FR-2023-1" and meta["countries"] == ["FR"]
    assert meta["year"] == 2023 and meta["policy_area"] is None and meta["programme"] is None
    assert meta["context_header"] == "France 2023 CSR 1 — 2023 — France"
    assert docs[0].id == "csr/csr-FR-2023-1#0"


def test_read_rows_from_xlsx(tmp_path):
    path = tmp_path / "csr.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    wb.active.append([row()[c] for c in COLUMNS])
    wb.save(path)
    rows = read_rows(path)
    assert rows == [row()]


def test_latest_assessment_skips_not_assessed():
    rows = [row(nbr="1", version=f"{y} - Semester Cycle (June)", assessment=a, year="2021", areas="Healthcare")
            for y, a in ((2022, "Some Progress"), (2023, "Substantial Progress"), (2024, "Not Assessed"),
                         (2025, "Not Assessed"))]
    csr = build_csrs(rows)[0]
    assert csr.assessment == "Substantial Progress"
    body = parse_front_matter(render_csr(csr))[1]
    assert "Latest assessment (2023): Substantial Progress." in body
    assert "2024: Not Assessed; 2025: Not Assessed" in body
    only = build_csrs([row(nbr="1", assessment="Not Assessed")])[0]
    assert only.assessment == "Not Assessed"


def test_year_number_robustness_and_ordering():
    rows = [row(year=2023, nbr="1"), row(year=2022.0, nbr="1"), row(nbr="1a"), row(nbr="2.0", text="Full two."),
            row(nbr="3.10", text="ten"), row(nbr="3.9", text="nine")]
    csrs = {c.csr_id: c for c in build_csrs(rows)}
    assert "csr-FR-2023-1" in csrs and "csr-FR-2022-1" in csrs
    assert not any(c.number == "1a" for c in csrs.values())
    assert csrs["csr-FR-2023-2"].full is not None and csrs["csr-FR-2023-2"].number == "2"
    assert [p.part_id for p in csrs["csr-FR-2023-3"].subparts] == ["csr-FR-2023-3.9", "csr-FR-2023-3.10"]
