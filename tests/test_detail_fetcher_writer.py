from conftest import FakeFetcher, fixture_bytes
from tsi_agent.components.detail_fetcher import DetailPageFetcher
from tsi_agent.components.writer import MarkdownProjectWriter
from tsi_agent.models import ProgrammeDoc, ProjectContent, ProjectRecord, ProjectRef
from tsi_agent.render import parse_front_matter
from tsi_agent.store import LocalProjectStore, StoreEntry

URL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/health-and-long-term-care/hems_en"


def ref(**kw):
    base = dict(project_id="hems", title="HEMS", programme="TSI", year=2022, countries=["HR"], detail_url=URL,
                listing_urls=["https://x/croatia_en"])
    return ProjectRef(**{**base, **kw})


def fetcher(tmp_path, store):
    f = DetailPageFetcher(output_dir=str(tmp_path))
    f._fetcher = FakeFetcher(pages={URL: fixture_bytes("detail_hems_croatia.html")})
    f._store = store
    return f


def writer(store):
    w = MarkdownProjectWriter()
    w._store = store
    return w


def test_detail_fetcher_new_then_unchanged_then_full(tmp_path):
    store = LocalProjectStore(tmp_path)
    f = fetcher(tmp_path, store)
    out = f.run(refs=[ref()])
    page = out["pages"][0]
    assert page.title == "Establishment of Helicopter Emergency Medical Services in Croatia"
    assert (page.programme, page.year) == ("TSI", 2022) and page.content_hash.startswith("sha256:")
    store.put(StoreEntry(project_id="hems", file="f", content_hash=page.content_hash, first_seen="d", last_updated="d"))
    assert [r.project_id for r in f.run(refs=[ref()])["unchanged"]] == ["hems"]
    assert len(f.run(refs=[ref()], full=True)["pages"]) == 1


def test_detail_fetcher_failure(tmp_path):
    f = fetcher(tmp_path, LocalProjectStore(tmp_path))
    out = f.run(refs=[ref(project_id="missing", detail_url="https://x/missing_en")])
    assert out["failed"] == ["missing"] and out["pages"] == []


def record(**kw):
    content = ProjectContent(summary="S.", context="C.", support_delivered="D.", results_achieved="R.")
    return ProjectRecord(ref=ref(**kw), content=content, content_hash="sha256:1", model="claude-cli/sonnet")


def test_writer_thin_then_detail_replaces_thin_file(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    thin_ref = ProjectRef(project_id="tsi-2022-hems", title="HEMS", programme="TSI", year=2022, countries=["HR"],
                          listing_urls=["https://x/croatia_en"])
    w.run(run_id="r1", thin=[thin_ref])
    fm, body = parse_front_matter(store.read_project_file("tsi-2022-hems"))
    assert fm["detail_page"] is False and fm["extraction"] == {"run_id": "r1", "model": "none"}
    assert "No detail page is published." in body
    out = w.run(run_id="r2", records=[record(aliases=["tsi-2022-hems"])])
    assert out["stats"]["detail"] == 1
    assert store.read_project_file("tsi-2022-hems") is None and store.get("tsi-2022-hems") is None
    fm, body = parse_front_matter(store.read_project_file("hems"))
    assert fm["aliases"] == ["tsi-2022-hems"] and fm["content_hash"] == "sha256:1"
    assert fm["extraction"] == {"run_id": "r2", "model": "claude-cli/sonnet"}
    assert "## Results achieved\n\nR." in body
    assert store.get("hems").first_seen == store.get("hems").last_updated


def test_writer_refreshes_front_matter_of_unchanged_and_delists(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    w.run(run_id="r1", records=[record()])
    out = w.run(run_id="r2", unchanged=[ref(spotlight=True)])
    assert out["stats"]["refreshed"] == 1
    fm, body = parse_front_matter(store.read_project_file("hems"))
    assert fm["spotlight"] is True and fm["extraction"]["run_id"] == "r1" and "## Summary\n\nS." in body
    assert w.run(run_id="r3", unchanged=[ref(spotlight=True)])["stats"]["refreshed"] == 0
    w.run(run_id="r4", delisted=["hems"])
    fm, _ = parse_front_matter(store.read_project_file("hems"))
    assert fm["listed"] is False and store.get("hems").listed is False


def test_writer_programme_docs_only_when_changed(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    doc = ProgrammeDoc(doc_id="country-croatia", title="Croatia", url="https://x/croatia_en", text="x" * 400)
    assert w.run(run_id="r1", programme_docs=[doc])["stats"]["programme_docs"] == 1
    assert w.run(run_id="r2", programme_docs=[doc])["stats"]["programme_docs"] == 0
    assert (tmp_path / "programme" / "country-croatia.md").exists()
