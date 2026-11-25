from types import SimpleNamespace

import openpyxl

from tsi_agent.csr import COLUMNS, COMMISSION_STAGE, build_documents
from tsi_agent.csr_commission import (PUBLICATION_ID, PUBLICATION_URL, document_country, fetch_missing,
                                      find_source, load, load_all_csrs, merge_texts, parse_text,
                                      split_recommendations)

HR_URL = "https://ec.example/download/1_en?filename=Croatia-COM_2026_211_1_EN_ACT_part1_v4.pdf"
NL_URL = "https://ec.example/download/2_en?filename=NL-COM_2026_219_1_EN_ACT_part1_v4.pdf"

HR = """(20) Addressing these challenges would support convergence (17).
HEREBY RECOMMENDS that Croatia take action in 2026 and 2027 to:
1. Adhere to the maximum growth rates of net expenditure recommended by the
Council on 21 January 2025. Reinforce defence spending and readiness while
17 SWD(2026)122 – Second-stage country analysis on social convergence in line with the Social
Convergence Framework (SCF), 2026.
EN EN
9

--- page 10 ---
ensuring spending efficiency. Accelerate liquidation-
related court proceedings.
2. Ensure continuity of reforms and investments implemented under the Recovery and
Resilience Facility.
3. Promote the consolidation of municipalities, raising the share to
4. per cent of GDP while improving 2. targeted support.
Done at Brussels,
For the Council
The President
EN EN
11
"""

NL = """HEREBY RECOMMENDS that the Netherlands take action in 2026 and 2027 to:
1. Align the taxation of different types of income from wealth.
2. Ensure continuity of reforms.
Done at Brussels,
"""

TEXT = f"""---
publication_id: {PUBLICATION_ID}
---
# 2026 European Semester

## 2026 European Semester: Country Specific Recommendation / Commission Recommendation - Croatia

Source: {HR_URL}

--- page 9 ---
{HR}
## 2026 European Semester: Country Specific Recommendation / Commission Recommendation - Netherlands

Source: {NL_URL}

--- page 1 ---
{NL}
## 2026 European Semester: Country Specific Recommendation / Commission Recommendation - Austria

Source: https://ec.example/AT.pdf

--- page 1 ---
Recitals only, the recommendations part could not be read.
"""


def test_parse_text_numbered_recommendations_per_country():
    result = parse_text(TEXT)
    ids = [c.csr_id for c in result.csrs]
    assert ids == ["csr-HR-2026-1", "csr-HR-2026-2", "csr-HR-2026-3", "csr-NL-2026-1", "csr-NL-2026-2"]
    hr1, hr2, hr3 = result.csrs[:3]
    # pied de page, numero de page et note de bas de page (17) retires ; trait d'union garde
    assert hr1.text == ("Adhere to the maximum growth rates of net expenditure recommended by the Council on "
                        "21 January 2025. Reinforce defence spending and readiness while ensuring spending "
                        "efficiency. Accelerate liquidation-related court proceedings.")
    assert hr2.text.endswith("Recovery and Resilience Facility.")
    # « 4. per cent » : suite attendue mais minuscule -> texte de la recommandation 3
    assert hr3.text == "Promote the consolidation of municipalities, raising the share to 4. per cent of GDP while "                        "improving 2. targeted support."
    assert hr1.stage == COMMISSION_STAGE and hr1.source_url == HR_URL and hr1.year == 2026
    assert hr1.subparts == [] and hr1.full.part_id == "csr-HR-2026-1" and hr1.assessment is None
    report = result.report()
    assert report["by_country"] == {"HR": 3, "NL": 2, "AT": 0}
    assert report["alerts"] == ["AT: no 'HEREBY RECOMMENDS ... to:' part"]
    assert "AT" in report["missing_countries"] and "HR" not in report["missing_countries"]


def test_out_of_sequence_number_stays_in_the_text():
    recs = split_recommendations("1. First line\n3. not a new one\n2. Second\n2. again text")
    assert recs == [("1", "First line 3. not a new one"), ("2", "Second 2. again text")]


def test_document_country_from_title_sentence_or_filename():
    assert document_country("Commission Recommendation - Croatia", "", "") == "HR"
    assert document_country("Recommendation", "", "HEREBY RECOMMENDS that the Netherlands take action in 2026 to:") == "NL"
    assert document_country("Recommendation", "https://x/d?filename=EL-COM_2026.pdf", "") == "GR"
    assert document_country("Recommendation", "https://x/d?filename=Lithu-COM.pdf", "") is None


def test_unknown_country_and_duplicates_are_alerts():
    text = TEXT + f"\n## Second copy - Croatia\n\nSource: {HR_URL}\n\n{NL}\n## Unknown\n\nSource: https://x/y.pdf\n\nNo.\n"
    alerts = parse_text(text).report()["alerts"]
    assert "HR: duplicate document for this country (ignored)" in alerts
    assert "Unknown: country not recognised" in alerts


def test_documents_layer_for_commission_recommendations():
    docs = build_documents(parse_text(TEXT).csrs)
    assert [d.doc_id for d in docs] == ["csrdoc-HR-2026", "csrdoc-NL-2026"]
    assert docs[1].stage == COMMISSION_STAGE and docs[1].document_url == NL_URL and docs[1].n_recommendations == 2


def _xlsx(path):
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    wb.active.append(["Croatia", "2025", "1", "2025 - Semester Cycle (June)", "Text.", "Taxation", "-", "No",
                      "Annual", "Some Progress"])
    wb.save(path)


def test_sources_priority_and_load_all(tmp_path):
    assert find_source(tmp_path) is None and load(tmp_path).csrs == []
    (tmp_path / "deliverables").mkdir()
    (tmp_path / "deliverables" / f"{PUBLICATION_ID}.md").write_text(TEXT, encoding="utf-8")
    assert find_source(tmp_path) == tmp_path / "deliverables" / f"{PUBLICATION_ID}.md"
    kept = tmp_path / "csr" / "publications" / f"{PUBLICATION_ID}.md"
    kept.parent.mkdir(parents=True)
    kept.write_text(NL.join(["## X - Netherlands\n\nSource: " + NL_URL + "\n\n", ""]), encoding="utf-8")
    assert find_source(tmp_path) == kept
    assert [c.csr_id for c in load(tmp_path).csrs] == ["csr-NL-2026-1", "csr-NL-2026-2"]
    _xlsx(tmp_path / "csr" / "csr_full.xlsx")
    assert [c.csr_id for c in load_all_csrs(tmp_path)] == ["csr-HR-2025-1", "csr-NL-2026-1", "csr-NL-2026-2"]
    assert [c.csr_id for c in load_all_csrs(tmp_path, include_commission=False)] == ["csr-HR-2025-1"]


PAGE = f"""<html><body><main><h1>2026 European Semester</h1>
<div class="ecl-file"><div class="ecl-file__title">Commission Recommendation - Croatia</div>
<a class="ecl-file__download" href="{HR_URL.replace('https://ec.example', 'https://reforms-investments.ec.europa.eu')}">Download</a>
<ul><li class="ecl-file__translation-item"><a class="ecl-file__translation-download"
 href="https://reforms-investments.ec.europa.eu/download/1_hr?filename=Croatia-HR.pdf">hr</a></li></ul></div>
<div class="ecl-file"><div class="ecl-file__title">Commission Recommendation - Netherlands</div>
<a class="ecl-file__download" href="https://reforms-investments.ec.europa.eu/download/2_en?filename=NL-COM.pdf">Download</a></div>
<div class="ecl-file"><div class="ecl-file__title">Commission Recommendation - Austria</div>
<a class="ecl-file__download" href="https://reforms-investments.ec.europa.eu/download/3_en?filename=AT-COM.pdf">Download</a></div>
</main></body></html>"""


class FakeFetcher:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def get(self, url, cache=True):
        self.calls.append((url, cache))
        return SimpleNamespace(content=PAGE.encode() if url == PUBLICATION_URL else self.files[url])


def test_fetch_missing_downloads_only_missing_english_pdfs():
    hr_en = HR_URL.replace("https://ec.example", "https://reforms-investments.ec.europa.eu")
    fetcher = FakeFetcher({hr_en: b"%PDF hr", "https://reforms-investments.ec.europa.eu/download/3_en?filename=AT-COM.pdf":
                           b"not a pdf"})
    text = fetch_missing(fetcher, ["HR", "AT"], reader=lambda data: (1, "--- page 1 ---\n" + HR))
    assert [u for u, _ in fetcher.calls] == [PUBLICATION_URL, hr_en,
                                             "https://reforms-investments.ec.europa.eu/download/3_en?filename=AT-COM.pdf"]
    assert all(cache is False for _, cache in fetcher.calls[1:])
    assert text.startswith(f"## Commission Recommendation - Croatia\n\nSource: {hr_en}\n\n")
    merged = merge_texts(TEXT, text)
    assert merged.startswith(TEXT.rstrip("\n")) and merged.count("- Croatia") == 2
    assert merge_texts(None, text).startswith(f"# {PUBLICATION_ID}\n\n## Commission")
    assert merge_texts("x", "") == "x"
    assert [c.csr_id for c in parse_text(merge_texts(None, text)).csrs][:1] == ["csr-HR-2026-1"]


# Considerants (recitals) -------------------------------------------------------------------------------------------

RECITALS = """Whereas:
(1) Regulation (EU) 2024/1263 specifies the objectives of the economic governance
framework (1).
(2) On 16 July 2025, the Commission adopted its proposal for a regulation (2). The proposal
1 Regulation (EU) 2024/1263 of the European Parliament and of the Council of 29 April 2024 on the
effective coordination of economic policies.
2 Proposal for a Regulation of the European Parliament and of the Council establishing the European
EN EN
1

--- page 3 ---
aims to increase the effectiveness of Union funding, in line with recommendation (1) below.
(3) Wildfires and droughts are a growing risk for agriculture and water supply (3).
Regulation (EU, Euratom) 2024/2509 - COM(2025) 565 final. The proposed Regulation is currently the
subject of negotiations with the co-legislators.
3 Commission report on rural fires and water resilience, 2026.
EN EN
2

--- page 4 ---
HEREBY RECOMMENDS that Austria take action in 2026 and 2027 to:
1. Improve water management.
Done at Brussels,
"""

PAREN_NOTES = """Whereas:
(1) The Commission published the 2026 Annual Progress Report (8) for Ireland.
(2) Ireland faces challenges related to housing.
(8) The 2026 Annual Progress Reports are available on: https://economy-finance.ec.europa.eu/
(9) Eurostat-Euro Indicators, 22 April 2026.
EN EN
3

--- page 4 ---
(3) Housing supply remains constrained.
HEREBY RECOMMENDS that Ireland take action in 2026 and 2027 to:
1. Increase housing supply.
Done at Brussels,
"""


def _doc(country: str, body: str) -> str:
    return (f"## 2026 European Semester: Country Specific Recommendation / Commission Recommendation - {country}\n\n"
            f"Source: https://ec.example/{country}.pdf\n\n--- page 2 ---\n{body}\n")


def test_recitals_split_and_strip_footnotes():
    parse = parse_text(_doc("Austria", RECITALS))
    recs = parse.recitals
    assert [r.recital_id for r in recs] == ["rec-AT-2026-1", "rec-AT-2026-2", "rec-AT-2026-3"]
    assert recs[0].text == ("Regulation (EU) 2024/1263 specifies the objectives of the economic governance framework (1).")
    # note de la page 2 retiree ; suite du considerant sur la page 3 recollee ; « (1) » en milieu de ligne garde
    assert recs[1].text.endswith("The proposal aims to increase the effectiveness of Union funding, in line with "
                                 "recommendation (1) below.")
    # suite d'une note commencee page precedente (ligne large) et note numerotee retirees
    assert recs[2].text == "Wildfires and droughts are a growing risk for agriculture and water supply (3)."
    assert recs[2].country == "AT" and recs[2].year == 2026 and recs[2].source_url == "https://ec.example/Austria.pdf"
    assert parse.report()["recitals"] == 3


def test_recitals_parenthesised_footnotes():
    recs = parse_text(_doc("Ireland", PAREN_NOTES)).recitals
    assert [r.number for r in recs] == [1, 2, 3]
    assert recs[1].text == "Ireland faces challenges related to housing."
    assert recs[2].text == "Housing supply remains constrained."


def test_recitals_absent_without_whereas():
    assert parse_text(_doc("Netherlands", NL)).recitals == []


def test_adopted_csrs_replace_commission_recommendations_per_country():
    from tsi_agent.csr import Csr, Part
    from tsi_agent.csr_commission import with_commission
    commission = [Csr(c, 2026, "1", Part(f"csr-{c}-2026-1", "Text.", [], []), [], stage=COMMISSION_STAGE)
                  for c in ("HR", "NL")]
    adopted = [Csr("HR", 2026, "1", Part("csr-HR-2026-1", "Adopted.", [], []), [])]
    merged = with_commission(adopted, commission)
    assert [(c.country, c.stage == COMMISSION_STAGE) for c in merged] == [("HR", False), ("NL", True)]
