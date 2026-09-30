import pytest

from conftest import FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.detail_fetcher import DetailPageFetcher
from tsi_agent.components.writer import MarkdownProjectWriter
from tsi_agent.http import FetchError
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


@pytest.mark.parametrize("status", [404, 410])
def test_detail_fetcher_alerts_when_page_is_gone(tmp_path, status):
    f = DetailPageFetcher(output_dir=str(tmp_path))
    f._store = LocalProjectStore(tmp_path)

    class Gone(FakeFetcher):
        def get(self, url):
            raise FetchError(url, f"HTTP {status}", status)

    f._fetcher = Gone()
    with runlog.open_run(tmp_path / "logs", f"r{status}") as run:
        out = f.run(refs=[ref(project_id="missing", detail_url="https://x/missing_en")])
    assert out["failed"] == ["missing"]
    assert any(f"Detail page gone ({status})" in a and "https://x/missing_en" in a for a in run.alerts)


def test_detail_fetcher_os_error_is_not_fatal(tmp_path):
    f = DetailPageFetcher(output_dir=str(tmp_path))
    f._store = LocalProjectStore(tmp_path)

    class DiskFull(FakeFetcher):
        def get(self, url):
            raise OSError("disk full")

    f._fetcher = DiskFull()
    out = f.run(refs=[ref(project_id="missing", detail_url="https://x/missing_en")])
    assert out["failed"] == ["missing"] and out["pages"] == []


def test_detail_fetcher_does_not_alert_on_other_errors(tmp_path):
    f = DetailPageFetcher(output_dir=str(tmp_path))
    f._store = LocalProjectStore(tmp_path)

    class ServerError(FakeFetcher):
        def get(self, url):
            raise FetchError(url, "HTTP 503", 503)

    f._fetcher = ServerError()
    with runlog.open_run(tmp_path / "logs", "r503") as run:
        out = f.run(refs=[ref(project_id="missing", detail_url="https://x/missing_en")])
    assert out["failed"] == ["missing"]
    assert run.alerts == []


def record(listing_ref=None, **kw):
    """`listing_ref` : vue listing (par defaut identique au ref du record)."""
    content = ProjectContent(summary="S.", context="C.", support_delivered="D.", results_achieved="R.")
    rec_ref = ref(**kw)
    return ProjectRecord(ref=rec_ref, listing_hash=(listing_ref or rec_ref).listing_hash(), content=content,
                         content_hash="sha256:1", model="claude-cli/sonnet")


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


def test_first_seen_is_kept_from_the_replaced_thin_entry(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    thin_entry = StoreEntry(project_id="tsi-2022-hems", file="projects/tsi-2022-hems.md", detail_page=False,
                            first_seen="2025-01-15", last_updated="2025-01-15")
    store.put(thin_entry)
    store.write_project_file("tsi-2022-hems", "# HEMS\n\nplaceholder\n")
    w.run(run_id="r2", records=[record(aliases=["tsi-2022-hems"])])
    assert store.get("hems").first_seen == "2025-01-15"
    fm, _ = parse_front_matter(store.read_project_file("hems"))
    assert fm["first_seen"] == "2025-01-15"


def test_detail_alias_is_never_deleted(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    other_entry = StoreEntry(project_id="other", file="projects/other.md", detail_page=True,
                             first_seen="2026-09-30", last_updated="2026-09-30")
    store.put(other_entry)
    store.write_project_file("other", "# Other\n\nsome content\n")
    w.run(run_id="r2", records=[record(aliases=["other"])])
    assert store.read_project_file("other") is not None
    assert store.get("other") is not None


def test_refresh_updates_listing_sections_and_aliases(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    w.run(run_id="r1", records=[record()])
    w.run(run_id="r2", unchanged=[ref(listing_titles=["HEMS for Croatia"],
                                      listing_urls=["https://x/croatia_en", "https://x/spotlight_en"],
                                      aliases=["tsi-2022-hems"])])
    fm, body = parse_front_matter(store.read_project_file("hems"))
    assert "## Listed in country factsheets as\n\n- HEMS for Croatia" in body
    assert "- https://x/spotlight_en" in body
    assert "## Summary\n\nS." in body
    assert fm["aliases"] == ["tsi-2022-hems"]
    assert store.get("hems").aliases == ["tsi-2022-hems"]


def test_writer_programme_docs_only_when_changed(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    doc = ProgrammeDoc(doc_id="country-croatia", title="Croatia", url="https://x/croatia_en", text="x" * 400)
    assert w.run(run_id="r1", programme_docs=[doc])["stats"]["programme_docs"] == 1
    assert w.run(run_id="r2", programme_docs=[doc])["stats"]["programme_docs"] == 0
    assert (tmp_path / "programme" / "country-croatia.md").exists()


def test_llm_countries_survive_an_identical_second_run(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    listing = ref(countries=[])  # le listing ne donne pas de pays ; le LLM a trouvé HR
    w.run(run_id="r1", records=[record(listing_ref=listing, countries=["HR"])])
    before = store.read_project_file("hems")
    out = w.run(run_id="r2", unchanged=[listing])
    assert out["stats"]["refreshed"] == 0
    assert store.read_project_file("hems") == before
    assert store.get("hems").countries == ["HR"]


def test_refresh_keeps_stored_countries_when_listing_has_none(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    listing = ref(countries=[])
    w.run(run_id="r1", records=[record(listing_ref=listing, countries=["HR"])])
    out = w.run(run_id="r2", unchanged=[listing.model_copy(update={"spotlight": True})])
    assert out["stats"]["refreshed"] == 1
    fm, _ = parse_front_matter(store.read_project_file("hems"))
    assert fm["spotlight"] is True and fm["countries"] == ["HR"]
    assert store.get("hems").countries == ["HR"]


def test_unchanged_detail_page_removes_its_thin_alias(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    w.run(run_id="r1", records=[record()])
    thin_ref = ProjectRef(project_id="tsi-2022-hems", title="HEMS", programme="TSI", year=2022, countries=["HR"],
                          listing_urls=["https://x/croatia_en"])
    w.run(run_id="r2", thin=[thin_ref])  # la page détail avait disparu
    assert store.get("tsi-2022-hems") is not None
    w.run(run_id="r3", unchanged=[ref(aliases=["tsi-2022-hems"])])
    assert store.get("tsi-2022-hems") is None and store.read_project_file("tsi-2022-hems") is None


def test_write_failure_on_one_project_does_not_kill_the_run(tmp_path):
    class FlakyStore(LocalProjectStore):
        def write_project_file(self, project_id, text):
            if project_id == "bad":
                raise OSError("disk full")
            return super().write_project_file(project_id, text)

    store = FlakyStore(tmp_path)
    w = writer(store)
    good = ProjectRef(project_id="good", title="Good", programme="TSI", year=2022, countries=["HR"])
    bad = ProjectRef(project_id="bad", title="Bad", programme="TSI", year=2022, countries=["HR"])
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        out = w.run(run_id="r1", thin=[good, bad])
    assert out["stats"]["thin"] == 1 and out["stats"]["write_failed"] == 1
    assert store.get("good") is not None
    assert store.get("bad") is None
    assert run.counts["error:write_failed"] == 1


def test_rewriting_a_record_merges_stored_aliases(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    w.run(run_id="r1", records=[record(aliases=["tsi-2022-hems"])])
    w.run(run_id="r2", records=[record(aliases=["tsi-2021-hems"])])
    assert store.get("hems").aliases == ["tsi-2022-hems", "tsi-2021-hems"]
    fm, _ = parse_front_matter(store.read_project_file("hems"))
    assert fm["aliases"] == ["tsi-2022-hems", "tsi-2021-hems"]
