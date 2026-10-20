"""Publications CSR exclues des livrables de projet (deliverable_exclude_patterns de config/sources.yaml)."""

from conftest import FakeFetcher
from tsi_agent.components.deliverables import DeliverableWriter, PublicationDiscoverer, PublicationFetcher
from tsi_agent.config import Settings, load_sources
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
from tsi_agent.deliverables_state import DeliverableEntry, DeliverableStore, is_excluded
from tsi_agent.models import DeliverableContent, Publication
from tsi_agent.render import parse_front_matter, render
from tsi_agent.store import LocalProjectStore, StoreEntry

SITE = "https://reforms-investments.ec.europa.eu"
CSR_ID = "2026-european-semester-country-specific-recommendations-commission-recommendations"
CSR_PUB = f"{SITE}/publications-0/{CSR_ID}_en"
OTHER_PUB = f"{SITE}/publications-0/energy-regulation-academy-final-report_en"
DETAIL = f"{SITE}/technical-support-instrument-0/green-transition/energy-regulation-academy_en"
PATTERNS = ["country-specific-recommendations", "Country Specific Recommendation"]


def test_is_excluded_case_insensitive_on_any_value():
    assert is_excluded(PATTERNS, CSR_ID)
    assert is_excluded(PATTERNS, "x", "2025 European Semester: country specific recommendations")
    assert not is_excluded(PATTERNS, "energy-regulation-academy-final-report", "Final report")
    assert not is_excluded([], CSR_ID) and not is_excluded(None, CSR_ID)
    assert not is_excluded(["", "  "], CSR_ID)


def test_sources_yaml_lists_the_csr_patterns():
    patterns = load_sources("config/sources.yaml")["deliverable_exclude_patterns"]
    assert is_excluded(patterns, CSR_ID)
    assert is_excluded(patterns, "x", "2026 European Semester: Country Specific Recommendations / Commission Recommendations")


def store_with_project(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.put(StoreEntry(project_id="era", file="projects/era.md", detail_page=True, source_urls=[DETAIL],
                         first_seen="d", last_updated="d"), save=False)
    store.flush()
    fm = {"project_id": "era", "title": "Energy Regulation Academy", "results": "achieved"}
    store.write_project_file("era", render(fm, "# ERA\n\nIntro.\n\n## Sources\n\n- https://x/era"))
    return store


DETAIL_HTML = (f'<html><body><main><a href="/publications-0/{CSR_ID}_en">CSR</a>'
               '<a href="/publications-0/energy-regulation-academy-final-report_en">Report</a></main></body></html>').encode()


def test_discoverer_drops_excluded_publication_links(tmp_path):
    d = PublicationDiscoverer(output_dir=str(tmp_path), exclude_patterns=PATTERNS)
    d._store = store_with_project(tmp_path)
    d._fetcher = FakeFetcher(cached_pages={DETAIL: DETAIL_HTML})
    out = d.run()
    assert [p.url for p in out["publications"]] == [OTHER_PUB] and out["complete"] is True
    plain = PublicationDiscoverer(output_dir=str(tmp_path))
    plain._store, plain._fetcher = d._store, d._fetcher
    assert sorted(p.url for p in plain.run()["publications"]) == sorted([CSR_PUB, OTHER_PUB])


PUB_HTML = (b'<html><body><h1>2026 European Semester: Country Specific Recommendations</h1><main>'
            b'<div class="ecl-file"><div class="ecl-file__title">Doc</div>'
            b'<a class="ecl-file__download" href="/document/download/1_en?filename=a.pdf">D</a></div></main></body></html>')


def test_fetcher_skips_a_publication_whose_title_is_excluded(tmp_path):
    f = PublicationFetcher(output_dir=str(tmp_path), exclude_patterns=PATTERNS)
    f._fetcher = FakeFetcher(pages={OTHER_PUB: PUB_HTML})
    out = f.run(publications=[Publication(publication_id="renamed", url=OTHER_PUB, project_ids=["era"])])
    assert out["to_process"] == [] and out["unchanged"] == [] and out["failed"] == []


def csr_entry():
    return DeliverableEntry(
        publication_id=CSR_ID, url=CSR_PUB, title="2026 European Semester: Country Specific Recommendations",
        project_ids=["era"], files_key="k", files=[], full_text_path=f"deliverables/{CSR_ID}.md",
        content=DeliverableContent(summary="CSR."), model="m", processed="2026-06-10")


def test_writer_removes_excluded_publications_fixes_the_project_and_keeps_the_text_as_csr_source(tmp_path):
    store = store_with_project(tmp_path)
    state = DeliverableStore(tmp_path)
    state.put(csr_entry())
    state.save()
    text = tmp_path / "deliverables" / f"{CSR_ID}.md"
    text.parent.mkdir()
    text.write_text("---\npublication_id: x\n---\n# CSR\n", encoding="utf-8")
    plain = DeliverableWriter(output_dir=str(tmp_path))
    plain._store = store
    plain.run(records=[], unchanged=[])  # sans motif : rien ne change, le projet garde sa section
    assert DeliverableStore(tmp_path).get(CSR_ID) is not None
    w = DeliverableWriter(output_dir=str(tmp_path), exclude_patterns=PATTERNS)
    w._store = store
    stats = w.run(records=[], unchanged=[], complete=False)["stats"]
    assert stats["excluded_removed"] == 1
    assert DeliverableStore(tmp_path).get(CSR_ID) is None
    fm, body = parse_front_matter(store.read_project_file("era"))
    assert "deliverables" not in fm and "## Deliverables" not in body
    kept = tmp_path / "csr" / "publications" / f"{CSR_ID}.md"
    assert not text.exists() and kept.read_text(encoding="utf-8").endswith("# CSR\n")
    assert w.run(records=[])["stats"]["excluded_removed"] == 0  # idempotent


def test_writer_keeps_an_existing_csr_copy(tmp_path):
    store = store_with_project(tmp_path)
    state = DeliverableStore(tmp_path)
    state.put(csr_entry())
    state.save()
    (tmp_path / "deliverables").mkdir()
    (tmp_path / "deliverables" / f"{CSR_ID}.md").write_text("old", encoding="utf-8")
    kept = tmp_path / "csr" / "publications" / f"{CSR_ID}.md"
    kept.parent.mkdir(parents=True)
    kept.write_text("completed copy", encoding="utf-8")
    w = DeliverableWriter(output_dir=str(tmp_path), exclude_patterns=PATTERNS)
    w._store = store
    w.run(records=[])
    assert kept.read_text(encoding="utf-8") == "completed copy"
    assert not (tmp_path / "deliverables" / f"{CSR_ID}.md").exists()


def test_pipeline_passes_patterns_and_round_trips(tmp_path):
    settings = Settings(output_dir=str(tmp_path), deliverables_dir=str(tmp_path / "deliverables"),
                        logs_dir=str(tmp_path / "logs"))
    from conftest import FakeChatGenerator
    p = build_deliverables_pipeline(settings, chat_generator=FakeChatGenerator(), exclude_patterns=PATTERNS)
    for name in ("discoverer", "publications", "writer"):
        assert p.get_component(name).exclude_patterns == PATTERNS
    assert "country-specific-recommendations" in p.dumps()


def test_run_deliverables_and_cli_pass_the_patterns(monkeypatch, tmp_path):
    import pytest

    from tsi_agent import __main__ as cli
    from tsi_agent import runner
    seen = {}
    monkeypatch.setattr(cli, "_check_llm", lambda settings: True)
    monkeypatch.setattr(cli, "run_deliverables", lambda *a, **k: seen.update(k) or {"quota_exceeded": False})
    assert cli.main(["deliverables"]) == 0
    assert is_excluded(seen["exclude_patterns"], CSR_ID)
    built = {}

    class Stop(Exception):
        pass

    def factory(settings, **kw):
        built.update(kw)
        raise Stop

    with pytest.raises(Stop):
        runner.run_deliverables(Settings(output_dir=str(tmp_path), logs_dir=str(tmp_path / "logs")),
                                exclude_patterns=PATTERNS, pipeline_factory=factory, run_id="x")
    assert built["exclude_patterns"] == PATTERNS
