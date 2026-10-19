"""Liens de publication enregistres par `run` dans l'etat (StoreEntry.publication_urls), sans dependre du cache HTTP."""

from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.detail_fetcher import DetailPageFetcher
from tsi_agent.components.writer import MarkdownProjectWriter
from tsi_agent.models import ProjectContent, ProjectRecord, ProjectRef
from tsi_agent.store import LocalProjectStore, StoreEntry, merge_publication_urls

SITE = "https://reforms-investments.ec.europa.eu"
SLO_DETAIL = f"{SITE}/technical-support-instrument-0/health-and-long-term-care/developing-ehealth-healthier-society-slovenia_en"
SLO_PUB = f"{SITE}/publications-0/developing-ehealth-healthier-society-slovenia_en"
OTHER_PUB = f"{SITE}/publications-0/other_en"


def ref(**kw):
    base = dict(project_id="slo", title="SLO", programme="TSI", year=2022, countries=["SI"], detail_url=SLO_DETAIL,
                listing_urls=["https://x/slovenia_en"])
    return ProjectRef(**{**base, **kw})


def record(r):
    return ProjectRecord(ref=r, listing_hash=r.listing_hash(), content=ProjectContent(summary="S."),
                         content_hash="sha256:1", model="m", intro="I.", sections=[("Context", "C.")])


def test_merge_publication_urls_rules(tmp_path):
    assert merge_publication_urls("p", None, None) is None
    assert merge_publication_urls("p", [SLO_PUB], None) == [SLO_PUB]          # rien de lu : on garde
    assert merge_publication_urls("p", None, [SLO_PUB]) == [SLO_PUB]
    assert merge_publication_urls("p", [SLO_PUB], [OTHER_PUB]) == [OTHER_PUB]
    assert merge_publication_urls("p", None, []) == []
    with runlog.open_run(tmp_path / "logs", "r") as run:
        assert merge_publication_urls("p", [SLO_PUB], []) == [SLO_PUB]      # liste vide : suspect, on garde
    assert any("publication links" in a for a in run.alerts)


def test_detail_fetcher_puts_publication_links_on_changed_and_unchanged_refs(tmp_path):
    store = LocalProjectStore(tmp_path)
    f = DetailPageFetcher(output_dir=str(tmp_path))
    f._fetcher = FakeFetcher(pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html")})
    f._store = store
    page = f.run(refs=[ref()])["pages"][0]
    assert page.ref.publication_urls == [SLO_PUB]
    store.put(StoreEntry(project_id="slo", file="f", content_hash=page.content_hash, first_seen="d", last_updated="d"))
    unchanged = f.run(refs=[ref()])["unchanged"]
    assert [r.publication_urls for r in unchanged] == [[SLO_PUB]]


def writer(tmp_path):
    w = MarkdownProjectWriter()
    w._store = LocalProjectStore(tmp_path)
    return w


def test_writer_records_and_keeps_publication_links(tmp_path):
    w = writer(tmp_path)
    w.run(run_id="r1", records=[record(ref(publication_urls=[SLO_PUB]))])
    assert LocalProjectStore(tmp_path).get("slo").publication_urls == [SLO_PUB]
    w.run(run_id="r2", records=[record(ref())])  # re-extraction sans liens lus : on garde
    assert LocalProjectStore(tmp_path).get("slo").publication_urls == [SLO_PUB]
    w.run(run_id="r3", unchanged=[ref(publication_urls=[SLO_PUB, OTHER_PUB])])  # page relue, inchangee
    assert LocalProjectStore(tmp_path).get("slo").publication_urls == [SLO_PUB, OTHER_PUB]
    with runlog.open_run(tmp_path / "logs", "r4") as run:
        w.run(run_id="r4", unchanged=[ref(publication_urls=[])])
    assert LocalProjectStore(tmp_path).get("slo").publication_urls == [SLO_PUB, OTHER_PUB]
    assert run.alerts
    w.run(run_id="r5", skipped=[ref(publication_urls=[OTHER_PUB])])  # non relue : rien n'est enregistre
    assert LocalProjectStore(tmp_path).get("slo").publication_urls == [SLO_PUB, OTHER_PUB]


def test_run_records_publication_links_end_to_end(tmp_path):
    from test_pipeline import SOURCES, factory, fake_site
    from tsi_agent.config import Settings
    from tsi_agent.runner import run_extraction
    s = Settings(output_dir=str(tmp_path / "output"), logs_dir=str(tmp_path / "logs"),
                 cache_dir=str(tmp_path / "cache"), min_interval_s=0, chunk_size=20)
    run_extraction(s, SOURCES, countries=["HR"], run_id="r1",
                   pipeline_factory=factory(FakeChatGenerator(), FakeFetcher(default=fake_site)))
    detail = [e for e in LocalProjectStore(s.output_dir).all().values() if e.detail_page]
    assert detail and all(e.publication_urls is not None for e in detail)
    hems = f"{SITE}/publications-0/establishment-helicopter-emergency-medical-services-croatia_en"
    assert all(e.publication_urls == [hems] for e in detail)  # fake_site sert la page HEMS partout
