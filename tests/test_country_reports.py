from types import SimpleNamespace

from tsi_agent.country_reports import (Report, _gutter, chunks, fetch_reports, find_pdf, load_reports, page_url,
                                       report_path)
from tsi_agent.supabase_sync import build_rows

PAGE = """<a href="/document/download/aaa_en?filename=PT_SWD_2026_222_1_EN_autre_document_travail_service_part1_v3.pdf">
Download (4.5 MB - PDF)</a>
<a href="/document/download/aaa_pt">Download</a>
<a href="/document/download/bbb_en?filename=PT_CR_SWD_2025_222_1_EN_autre.pdf&amp;x=1">Download</a>"""

PDF_URL = ("https://economy-finance.ec.europa.eu/document/download/aaa_en?filename="
           "PT_SWD_2026_222_1_EN_autre_document_travail_service_part1_v3.pdf")

TEXT = ("--- page 1 ---\nEUROPEAN COMMISSION\n\n--- page 2 ---\n"
        + "Wildfires are a growing risk in Portugal. Water scarcity is a long-\nterm risk in the south. " * 12
        + "\nEN\n12\n\n--- page 3 ---\n" + "1.2 3.4 5.6 7.8 | 9.0 1.1 2.2 3.3 | 4.4 5.5 6.6 7.7\n" * 10)


def test_page_url_and_find_pdf():
    assert page_url("CZ").endswith("/country-report-czechia_en")
    assert find_pdf(PAGE) == PDF_URL
    assert find_pdf(PAGE, year=2027) is None
    assert find_pdf('<a href="/document/download/x_en?filename=SWD_2025_1.pdf">') is None


def test_chunks_join_lines_split_sentences_and_skip_tables_and_short_pages():
    report = Report("PT", 2026, PDF_URL, "https://page", 3, TEXT)
    out = chunks(report, size=300)
    assert {c.page for c in out} == {2}  # page 1 trop courte, page 3 = tableau de chiffres
    assert [c.chunk_id for c in out[:2]] == ["cr-PT-2026-p2-1", "cr-PT-2026-p2-2"]
    assert all(len(c.text) <= 300 for c in out)
    assert "a long-term risk" in out[0].text  # compose coupe en fin de ligne recolle, trait d'union garde
    assert all(c.text.startswith("Wildfires") for c in out)  # coupes entre phrases
    assert not any(c.text.endswith("12") for c in out)  # pied de page retire


def test_gutter_finds_the_column_gap():
    words = [{"x0": 60 + 20 * (i % 10), "x1": 75 + 20 * (i % 10)} for i in range(60)]
    words += [{"x0": 320 + 20 * (i % 10), "x1": 335 + 20 * (i % 10)} for i in range(60)]
    cut = _gutter(words, 0, 595)
    assert cut is not None and 255 < cut < 320
    assert _gutter(words[:60], 0, 595) is None or _gutter(words[:60], 0, 595) < 60 + 200  # une seule colonne


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, cache=True):
        self.calls.append((url, cache))
        return SimpleNamespace(content=self.pages[url])


def test_fetch_reports_writes_markdown_and_skips_existing(tmp_path):
    fetcher = FakeFetcher({page_url("PT"): PAGE.encode(), PDF_URL: b"%PDF-1.7 ...", page_url("ES"): b"<html></html>"})
    summary = fetch_reports(tmp_path, fetcher, ["PT", "ES"], reader=lambda data: (3, TEXT))
    assert summary["fetched"] == ["PT"] and "ES" in summary["failed"]
    assert (PDF_URL, False) in fetcher.calls  # PDF jamais mis en cache disque
    [report] = load_reports(tmp_path)
    assert (report.report_id, report.pages, report.source_url) == ("cr-PT-2026", 3, PDF_URL)
    assert report.text.strip() == TEXT.strip()
    assert report_path(tmp_path, "PT").exists()
    again = fetch_reports(tmp_path, fetcher, ["PT"], reader=lambda data: (3, TEXT))
    assert again["skipped"] == ["PT"] and again["fetched"] == []


def test_sync_rows_for_reports_and_passages(tmp_path):
    fetch_reports(tmp_path, FakeFetcher({page_url("PT"): PAGE.encode(), PDF_URL: b"%PDF"}), ["PT"],
                  reader=lambda data: (3, TEXT))
    (tmp_path / "projects").mkdir()
    rows = build_rows(tmp_path, current_year=2026)
    assert rows["country_reports"] == [{"report_id": "cr-PT-2026", "country": "PT", "year": 2026,
                                        "title": "Portugal — 2026 country report", "source_url": PDF_URL,
                                        "page_url": page_url("PT"), "pages": 3}]
    first = rows["country_report_passages"][0]
    assert first["passage_id"] == "cr-PT-2026-p2-1" and first["report_id"] == "cr-PT-2026" and first["page"] == 2


PAGE_2027 = PAGE.replace("PT_SWD_2026_222", "PT_SWD_2027_111")
PDF_2027 = PDF_URL.replace("PT_SWD_2026_222", "PT_SWD_2027_111")


def test_fetch_reports_keeps_the_previous_year_until_the_new_report_is_out(tmp_path):
    fetcher = FakeFetcher({page_url("PT"): PAGE.encode(), PDF_URL: b"%PDF-1.7 ..."})
    assert fetch_reports(tmp_path, fetcher, ["PT"], reader=lambda d: (3, TEXT), today=2026)["fetched"] == ["PT"]
    later = fetch_reports(tmp_path, fetcher, ["PT"], reader=lambda d: (3, TEXT), today=2027)
    assert later == {"fetched": [], "skipped": ["PT"], "failed": {}}   # 2027 not published yet: 2026 kept
    fetcher.pages.update({page_url("PT"): PAGE_2027.encode(), PDF_2027: b"%PDF-1.7 ..."})
    fetcher.calls.clear()
    out = fetch_reports(tmp_path, fetcher, ["PT"], reader=lambda d: (5, TEXT), today=2027)
    assert out["fetched"] == ["PT"] and (PDF_2027, False) in fetcher.calls
    assert report_path(tmp_path, "PT", 2026).exists() and report_path(tmp_path, "PT", 2027).exists()
    [report] = load_reports(tmp_path)   # latest year per country only
    assert (report.report_id, report.pages) == ("cr-PT-2027", 5)


def test_fetch_reports_failure_names_the_years_tried(tmp_path):
    fetcher = FakeFetcher({page_url("ES"): b"<html></html>"})
    out = fetch_reports(tmp_path, fetcher, ["ES"], today=2027)
    assert out["failed"]["ES"].startswith("no 2027 or 2026 English report link")
