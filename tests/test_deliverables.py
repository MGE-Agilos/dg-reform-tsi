import json

import pytest

from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.deliverables import (DeliverableSummarizer, DeliverableTextExtractor, DeliverableWriter,
                                               PublicationDiscoverer, PublicationFetcher)
from tsi_agent.config import Settings
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline, default_deliverables_generator
from tsi_agent.deliverables_state import DeliverableEntry, DeliverableStore
from tsi_agent.models import (DeliverableContent, DeliverableFileText, DeliverableRecord, DeliverableText,
                              Publication, PublicationFile)
from tsi_agent.pipeline import load_pipeline
from tsi_agent.render import (DELIVERABLES_HEADING, deliverables_section, parse_front_matter, render,
                              replace_deliverables_section, with_deliverables_count)
from tsi_agent.runner import run_deliverables
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
        out = d.run()
        pubs = out["publications"]
    assert {p.url: p.project_ids for p in pubs} == {
        SLO_PUB: ["slo"],
        f"{SITE}/publications-0/establishment-helicopter-emergency-medical-services-croatia_en": ["hems"]}
    assert pubs[0].publication_id in {"developing-ehealth-healthier-society-slovenia",
                                      "establishment-helicopter-emergency-medical-services-croatia"}
    assert d._fetcher.calls == []                       # aucune requete reseau
    assert any("not in the HTTP cache" in a for a in run.final_summary["alerts"])
    assert out["complete"] is False                      # la page detail de "gone" n'est pas en cache


def test_discoverer_complete_when_no_detail_page_is_missing(tmp_path):
    d = PublicationDiscoverer(output_dir=str(tmp_path))
    store = LocalProjectStore(tmp_path)
    store.put(StoreEntry(project_id="slo", file="projects/slo.md", detail_page=True, source_urls=[SLO_DETAIL],
                         first_seen="d", last_updated="d"), save=False)
    store.flush()
    d._store = store
    d._fetcher = FakeFetcher(cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html")})
    assert d.run()["complete"] is True


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


CONTENT = json.dumps({"summary": "The project delivered an eHealth strategy.", "key_outputs": ["Strategy"],
                      "results": ["Adopted by the ministry"], "figures": ["40+ stakeholders"]})


def text_of(words=600):
    pub = pub_with_file()
    ft = DeliverableFileText(file=pub.files[0], pages=2, words=words, text=" ".join(["word"] * words))
    return DeliverableText(publication=pub.model_copy(update={"files_key": "sha256:k"}), files=[ft],
                           full_text_path="deliverables/slo.md")


def test_summarizer_parses_json_and_truncates(tmp_path):
    gen = FakeChatGenerator(replies=[CONTENT])
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        out = DeliverableSummarizer(chat_generator=gen, max_words=50).run(texts=[text_of()])
    assert out["failed"] == [] and out["quota_exceeded"] is False
    rec = out["records"][0]
    assert rec.content.figures == ["40+ stakeholders"] and rec.model == "fake/model"
    prompt = gen.calls[0][-1].text
    assert "Developing eHealth" in prompt and "[... truncated" in prompt and prompt.count("word") <= 60
    assert run.final_summary["llm_calls"] == 1


def test_summarizer_retries_invalid_json_then_quota(tmp_path):
    gen = FakeChatGenerator(replies=["not json", CONTENT])
    assert len(DeliverableSummarizer(chat_generator=gen).run(texts=[text_of()])["records"]) == 1
    quota = FakeChatGenerator(error=LLMQuotaExceeded("usage limit reached"))
    out = DeliverableSummarizer(chat_generator=quota).run(texts=[text_of(), text_of()])
    assert out["quota_exceeded"] is True and out["records"] == []


def project_file(store, pid):
    fm = {"project_id": pid, "title": "T", "results": "achieved", "spotlight": False}
    store.write_project_file(pid, render(fm, f"# T\n\nIntro.\n\n## Context\n\nC.\n\n## Sources\n\n- https://x/{pid}"))


def record_for(pid="slo", summary="The project delivered an eHealth strategy."):
    return DeliverableRecord(text=text_of(), model="fake/model",
                             content=DeliverableContent(summary=summary, key_outputs=["Strategy"]))


def test_writer_updates_state_and_project_files(tmp_path):
    store = LocalProjectStore(tmp_path)
    project_file(store, "slo")
    w = DeliverableWriter(output_dir=str(tmp_path))
    w._store = store
    stats = w.run(records=[record_for()])["stats"]
    assert stats == {"publications": 1, "projects_updated": 1, "orphans_removed": 0}
    fm, body = parse_front_matter(store.read_project_file("slo"))
    assert fm["deliverables"] == 1 and list(fm).index("deliverables") == list(fm).index("results") + 1
    assert body.index("## Context") < body.index("## Deliverables (AI-generated summary)") < body.index("## Sources")
    assert "The project delivered an eHealth strategy." in body
    assert DeliverableStore(tmp_path).get("developing-ehealth-healthier-society-slovenia").project_ids == ["slo"]
    w.run(records=[record_for(summary="Updated summary.")])
    body = parse_front_matter(store.read_project_file("slo"))[1]
    assert body.count("## Deliverables (AI-generated summary)") == 1 and "Updated summary." in body
    assert w.run(records=[record_for(summary="Updated summary.")])["stats"]["projects_updated"] == 0


def test_writer_moves_section_when_project_list_changes(tmp_path):
    store = LocalProjectStore(tmp_path)
    project_file(store, "slo")
    project_file(store, "other")
    w = DeliverableWriter(output_dir=str(tmp_path))
    w._store = store
    w.run(records=[record_for()])
    moved = text_of().publication.model_copy(update={"project_ids": ["other"]})
    assert w.run(records=[], unchanged=[moved])["stats"]["projects_updated"] == 2
    assert "## Deliverables" not in store.read_project_file("slo")
    assert "## Deliverables (AI-generated summary)" in store.read_project_file("other")


def test_writer_removes_orphan_publications_when_discovery_is_complete(tmp_path):
    store = LocalProjectStore(tmp_path)
    project_file(store, "slo")
    w = DeliverableWriter(output_dir=str(tmp_path))
    w._store = store
    w.run(records=[record_for()])
    out = w.run(records=[], discovered=[], complete=True)   # plus aucun lien vers cette publication
    assert out["stats"]["orphans_removed"] == 1
    assert DeliverableStore(tmp_path).get("developing-ehealth-healthier-society-slovenia") is None
    fm, body = parse_front_matter(store.read_project_file("slo"))
    assert "deliverables" not in fm and "## Deliverables" not in body


def test_writer_keeps_publications_when_discovery_is_incomplete(tmp_path):
    store = LocalProjectStore(tmp_path)
    project_file(store, "slo")
    w = DeliverableWriter(output_dir=str(tmp_path))
    w._store = store
    w.run(records=[record_for()])
    out = w.run(records=[], discovered=[], complete=False)  # une page detail manquait au cache : pas de nettoyage
    assert out["stats"]["orphans_removed"] == 0
    assert DeliverableStore(tmp_path).get("developing-ehealth-healthier-society-slovenia") is not None
    assert "## Deliverables (AI-generated summary)" in store.read_project_file("slo")


def pipeline_settings(tmp_path):
    return Settings(output_dir=str(tmp_path), deliverables_dir=str(tmp_path / "deliverables"),
                    logs_dir=str(tmp_path / "logs"), chunk_size=1)


def wired_pipeline(settings, gen, tmp_path):
    p = build_deliverables_pipeline(settings, chat_generator=gen)
    store = project_store(tmp_path)
    project_file(store, "slo")
    fake = FakeFetcher(pages={SLO_PUB: fixture_bytes("pub_one_file.html"),
                              PDF_URL: fixture_bytes("deliverable_small.pdf")},
                       cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html")})
    for name in ("discoverer", "publications", "texts"):
        p.get_component(name)._fetcher = fake
    p.get_component("discoverer")._store = store
    p.get_component("writer")._store = store
    return p, store


def test_run_deliverables_end_to_end_then_incremental(tmp_path):
    s = pipeline_settings(tmp_path)
    gen = FakeChatGenerator(replies=[CONTENT])
    built = {}

    def factory(settings, **kw):
        built["p"], built["store"] = wired_pipeline(settings, gen, tmp_path)
        return built["p"]
    summary = run_deliverables(s, run_id="d1", pipeline_factory=factory)
    assert summary["status"] == "ok" and summary["quota_exceeded"] is False
    assert summary["written"]["publications"] == 1 and summary["remaining_publications"] == 0
    assert "## Deliverables (AI-generated summary)" in built["store"].read_project_file("slo")
    again = run_deliverables(s, run_id="d2", pipeline_factory=factory)
    assert again["written"]["publications"] == 0 and len(gen.calls) == 1


def test_run_deliverables_stops_on_quota(tmp_path):
    s = pipeline_settings(tmp_path)
    gen = FakeChatGenerator(error=LLMQuotaExceeded("usage limit reached"))
    summary = run_deliverables(s, run_id="q1", pipeline_factory=lambda settings, **kw:
                               wired_pipeline(settings, gen, tmp_path)[0])
    assert summary["quota_exceeded"] is True


def test_deliverables_pipeline_yaml_round_trip(tmp_path):
    text = build_deliverables_pipeline(pipeline_settings(tmp_path)).dumps()
    for name in ("PublicationDiscoverer", "PublicationFetcher", "DeliverableTextExtractor",
                 "DeliverableSummarizer", "DeliverableWriter", "ClaudeCliChatGenerator"):
        assert name in text
    assert load_pipeline(text).get_component("summarizer").max_words == 15000


def test_default_deliverables_generator_uses_its_own_timeout():
    assert default_deliverables_generator(Settings()).timeout_s == 600


DOCX_URL = f"{SITE}/document/download/af4c71d4-66a3-446a-8aab-c1772d4ce68c_en?filename=Narrative%20Report.docx"


def test_extractor_reads_word_documents_with_tables(tmp_path):
    pub = pub_with_file(DOCX_URL).model_copy(update={"files": [
        PublicationFile(document="Narrative report", url=DOCX_URL, language="en", filename="Narrative Report.docx")]})
    out = extractor(tmp_path, {DOCX_URL: fixture_bytes("deliverable_small.docx")}).run(publications=[pub])
    assert out["failed"] == [] and len(out["texts"]) == 1
    f = out["texts"][0].files[0]
    assert f.pages == 0 and f.words > 800                        # le texte est surtout dans des tableaux
    assert f.text.startswith("NARRATIVE REPORT") and "This project aimed to enhance the capacity" in f.text


def test_section_label_for_word_documents():
    e = entry()
    e.files[0].update({"filename": "r.docx", "pages": 0})
    assert "[Final Report (EN, DOCX)](https://x/d/1_en)" in deliverables_section([e])


def test_recently_processed_publications_are_not_rechecked(tmp_path):
    from datetime import date, timedelta
    ds = DeliverableStore(tmp_path)
    ds.put(entry(pid="developing-ehealth-healthier-society-slovenia", projects=("slo",)).model_copy(
        update={"processed": date.today().isoformat()}))
    ds.save()
    f = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html")})
    out = f.run(publications=[slo_pub()])
    assert out["to_process"] == [] and [p.publication_id for p in out["unchanged"]] == [
        "developing-ehealth-healthier-society-slovenia"]
    assert f._fetcher.calls == []                                 # aucune requete
    assert out["unchanged"][0].project_ids == ["slo"]
    old = (date.today() - timedelta(days=30)).isoformat()
    ds.put(ds.get("developing-ehealth-healthier-society-slovenia").model_copy(update={"processed": old}))
    ds.save()
    g = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html")})
    assert len(g.run(publications=[slo_pub()])["to_process"]) == 1 and g._fetcher.calls == [SLO_PUB]
    h = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html")})
    ds.put(ds.get("developing-ehealth-healthier-society-slovenia").model_copy(
        update={"processed": date.today().isoformat()}))
    ds.save()
    assert len(h.run(publications=[slo_pub()], full=True)["to_process"]) == 1
