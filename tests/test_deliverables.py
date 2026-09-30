import pytest

from conftest import FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.deliverables import DeliverableTextExtractor, PublicationDiscoverer, PublicationFetcher
from tsi_agent.deliverables_state import DeliverableEntry, DeliverableStore
from tsi_agent.models import DeliverableContent, Publication, PublicationFile
from tsi_agent.render import (DELIVERABLES_HEADING, deliverables_section, parse_front_matter,
                              replace_deliverables_section, with_deliverables_count)
from tsi_agent.store import LocalProjectStore, StoreEntry

SITE = "https://reforms-investments.ec.europa.eu"
SLO_DETAIL = f"{SITE}/technical-support-instrument-0/health-and-long-term-care/developing-ehealth-healthier-society-slovenia_en"
HEMS_DETAIL = f"{SITE}/technical-support-instrument-0/health-and-long-term-care/hems_en"
SLO_PUB = f"{SITE}/publications-0/developing-ehealth-healthier-society-slovenia_en"
PDF_URL = f"{SITE}/document/download/6ab9d519-2de1-499d-870b-77ae9a1458ca_en?filename=Final%20-%20deliverable.pdf"


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


def project_store(tmp_path):
    store = LocalProjectStore(tmp_path)
    for pid, url, detail in [("slo", SLO_DETAIL, True), ("hems", HEMS_DETAIL, True), ("gone", f"{SITE}/x_en", True),
                             ("thin", None, False)]:
        store.put(StoreEntry(project_id=pid, file=f"projects/{pid}.md", detail_page=detail,
                             source_urls=[url] if url else [], first_seen="d", last_updated="d"), save=False)
    store.flush()
    return store


def test_discoverer_reads_detail_pages_from_cache(tmp_path):
    d = PublicationDiscoverer(output_dir=str(tmp_path))
    d._store = project_store(tmp_path)
    d._fetcher = FakeFetcher(cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html"),
                                           HEMS_DETAIL: fixture_bytes("detail_hems_croatia.html")})
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        pubs = d.run()["publications"]
    assert {p.url: p.project_ids for p in pubs} == {
        SLO_PUB: ["slo"],
        f"{SITE}/publications-0/establishment-helicopter-emergency-medical-services-croatia_en": ["hems"]}
    assert pubs[0].publication_id in {"developing-ehealth-healthier-society-slovenia",
                                      "establishment-helicopter-emergency-medical-services-croatia"}
    assert d._fetcher.calls == []                       # aucune requete reseau
    assert any("not in the HTTP cache" in a for a in run.final_summary["alerts"])


def fetcher_component(tmp_path, pages):
    f = PublicationFetcher(output_dir=str(tmp_path))
    f._fetcher = FakeFetcher(pages=pages)
    return f


def slo_pub():
    return Publication(publication_id="developing-ehealth-healthier-society-slovenia", url=SLO_PUB,
                       project_ids=["slo"])


def test_publication_fetcher_new_unchanged_full(tmp_path):
    pages = {SLO_PUB: fixture_bytes("pub_one_file.html")}
    out = fetcher_component(tmp_path, pages).run(publications=[slo_pub()])
    assert len(out["to_process"]) == 1 and out["unchanged"] == [] and out["remaining"] == 0
    pub = out["to_process"][0]
    assert pub.title.startswith("Developing eHealth") and pub.files[0].url == PDF_URL and pub.files_key
    ds = DeliverableStore(tmp_path)
    ds.put(entry(pid=pub.publication_id).model_copy(update={"files_key": pub.files_key}))
    ds.save()
    out = fetcher_component(tmp_path, pages).run(publications=[slo_pub()])
    assert out["to_process"] == [] and len(out["unchanged"]) == 1
    assert len(fetcher_component(tmp_path, pages).run(publications=[slo_pub()], full=True)["to_process"]) == 1


def test_publication_fetcher_limit_offered_and_failures(tmp_path):
    other = Publication(publication_id="three", url=f"{SITE}/publications-0/three_en", project_ids=["p"])
    missing = Publication(publication_id="missing", url=f"{SITE}/publications-0/missing_en", project_ids=["p"])
    f = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html"),
                                     other.url: fixture_bytes("pub_three_files.html")})
    pubs = [slo_pub(), other, missing]
    first = f.run(publications=pubs, limit=1)
    assert [p.publication_id for p in first["to_process"]] == ["developing-ehealth-healthier-society-slovenia"]
    assert first["remaining"] == 2 and first["failed"] == []   # au-dela de la limite : ni recuperees ni comptees
    second = f.run(publications=pubs, limit=1)                  # meme instance : deja proposees exclues
    assert [p.publication_id for p in second["to_process"]] == ["three"] and second["remaining"] == 1
    third = f.run(publications=pubs, limit=1)
    assert third["to_process"] == [] and third["failed"] == ["missing"] and third["remaining"] == 0


def extractor(tmp_path, pages, **kw):
    x = DeliverableTextExtractor(output_dir=str(tmp_path), deliverables_dir=str(tmp_path / "deliverables"), **kw)
    x._fetcher = FakeFetcher(pages=pages)
    return x


def pub_with_file(url=PDF_URL):
    return slo_pub().model_copy(update={"title": "Developing eHealth", "date": "17 October 2023", "files": [
        PublicationFile(document="Final - deliverable", url=url, language="en", filename="Final - deliverable.pdf")]})


def test_extractor_writes_full_text_without_http_cache(tmp_path):
    x = extractor(tmp_path, {PDF_URL: fixture_bytes("deliverable_small.pdf")})
    out = x.run(publications=[pub_with_file()])
    assert out["failed"] == [] and len(out["texts"]) == 1
    t = out["texts"][0]
    assert t.files[0].pages == 2 and t.files[0].words > 500 and "PROJECT OBJECTIVES" in t.files[0].text
    assert t.full_text_path == "deliverables/developing-ehealth-healthier-society-slovenia.md"
    fm, body = parse_front_matter((tmp_path / t.full_text_path).read_text(encoding="utf-8"))
    assert fm["publication_id"] == "developing-ehealth-healthier-society-slovenia" and fm["projects"] == ["slo"]
    assert fm["files"][0]["pages"] == 2 and "--- page 1 ---" in body and "PROJECT OBJECTIVES" in body
    assert x._fetcher.uncached_calls == [PDF_URL]


@pytest.mark.parametrize("content,kw", [(b"<html>error</html>", {}), (b"%PDF-1.4 junk", {}),
                                        (None, {"max_pdf_mb": 0.0001})])
def test_extractor_skips_bad_files(tmp_path, content, kw):
    pdf = fixture_bytes("deliverable_small.pdf") if content is None else content
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        out = extractor(tmp_path, {PDF_URL: pdf}, **kw).run(publications=[pub_with_file()])
    assert out["texts"] == [] and out["failed"] == ["developing-ehealth-healthier-society-slovenia"]
    assert run.final_summary["alerts"]
