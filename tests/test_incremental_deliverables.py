"""Etage livrables incremental : la date <lastmod> du sitemap evite de relire les pages de publication inchangees."""

from datetime import date, timedelta

import pytest

from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from test_deliverables import (CONTENT, HEMS_DETAIL, SLO_DETAIL, SLO_PUB, entry, fetcher_component, pipeline_settings,
                               project_file, project_store, slo_pub)
from tsi_agent import __main__ as cli
from tsi_agent import runlog
from tsi_agent.components.deliverables import DeliverableWriter, PublicationDiscoverer
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
from tsi_agent.deliverables_state import DeliverableStore
from tsi_agent.models import Publication
from tsi_agent.runner import run_deliverables
from tsi_agent.store import LocalProjectStore, StoreEntry

SITEMAP = "https://reforms-investments.ec.europa.eu/sitemap.xml"
PDF = ("https://reforms-investments.ec.europa.eu/document/download/6ab9d519-2de1-499d-870b-77ae9a1458ca_en"
       "?filename=Final%20-%20deliverable.pdf")
SLO_ID = "developing-ehealth-healthier-society-slovenia"
OLD = (date.today() - timedelta(days=60)).isoformat()


def urlset(dates: dict[str, str]) -> bytes:
    body = "".join(f"<url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in dates.items())
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>").encode()


def slo_store(tmp_path, **kw):
    store = LocalProjectStore(tmp_path)
    store.put(StoreEntry(project_id="slo", file="projects/slo.md", detail_page=True, source_urls=[SLO_DETAIL],
                         first_seen="d", last_updated="d", **kw))
    return store


def known(tmp_path, lastmod, processed=OLD, files_key="sha256:k"):
    ds = DeliverableStore(tmp_path)
    ds.put(entry(pid=SLO_ID, projects=("slo",)).model_copy(
        update={"url": SLO_PUB, "processed": processed, "sitemap_lastmod": lastmod, "files_key": files_key}))
    ds.save()


def pub(lastmod=None):
    return slo_pub().model_copy(update={"sitemap_lastmod": lastmod})


# --- decouverte : dates du sitemap et liens de publication sans cache HTTP ------------------------------------------

def test_discoverer_puts_sitemap_dates_on_publications_and_reads_the_sitemap_once(tmp_path):
    d = PublicationDiscoverer(output_dir=str(tmp_path), sitemap_url=SITEMAP)
    d._store = project_store(tmp_path)
    d._fetcher = FakeFetcher(pages={SITEMAP: urlset({SLO_PUB: "2025-12-01T15:25Z"})},
                             cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html"),
                                           HEMS_DETAIL: fixture_bytes("detail_hems_croatia.html")})
    pubs = {p.url: p.sitemap_lastmod for p in d.run()["publications"]}
    assert pubs[SLO_PUB] == "2025-12-01T15:25Z"
    assert [v for k, v in pubs.items() if k != SLO_PUB] == [None]  # pas dans le sitemap
    d.run()  # lot suivant du meme run
    assert d._fetcher.calls == [SITEMAP]


def test_discoverer_without_sitemap_or_with_a_broken_one_leaves_dates_empty(tmp_path):
    for sitemap_url, pages in ((None, {}), (SITEMAP, {})):  # pas configure ; indisponible (404)
        d = PublicationDiscoverer(output_dir=str(tmp_path), sitemap_url=sitemap_url)
        d._store = slo_store(tmp_path)
        d._fetcher = FakeFetcher(pages=pages, cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html")})
        assert [p.sitemap_lastmod for p in d.run()["publications"]] == [None]


def test_discoverer_remembers_publication_links_for_pages_not_downloaded_again(tmp_path):
    store = slo_store(tmp_path)
    d = PublicationDiscoverer(output_dir=str(tmp_path))
    d._store = store
    d._fetcher = FakeFetcher(cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html")})
    d.run()
    assert LocalProjectStore(tmp_path).get("slo").publication_urls == [SLO_PUB]
    # Run suivant : page detail non relue (date du sitemap inchangee), cache HTTP vide.
    e = PublicationDiscoverer(output_dir=str(tmp_path))
    e._store = LocalProjectStore(tmp_path)
    e._fetcher = FakeFetcher()
    with runlog.open_run(tmp_path / "logs", "r2") as run:
        out = e.run()
    assert [(p.url, p.project_ids) for p in out["publications"]] == [(SLO_PUB, ["slo"])]
    assert out["complete"] is True and run.alerts == []


def test_discoverer_falls_back_to_known_publications_for_older_states(tmp_path):
    store = slo_store(tmp_path)  # etat d'avant `publication_urls`
    known(tmp_path, None)
    d = PublicationDiscoverer(output_dir=str(tmp_path))
    d._store = store
    d._fetcher = FakeFetcher()
    out = d.run()
    assert [(p.url, p.project_ids) for p in out["publications"]] == [(SLO_PUB, ["slo"])]
    assert out["complete"] is True


# --- PublicationFetcher : pas de telechargement pour une publication connue inchangee ------------------------------

def test_known_publication_with_same_or_older_date_is_skipped_even_if_not_recent(tmp_path):
    for lastmod in ("2025-12-01T15:25Z", "2025-01-01T00:00Z"):
        known(tmp_path, "2025-12-01T15:25Z")
        f = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html")})
        with runlog.open_run(tmp_path / "logs", f"r{lastmod[:4]}") as run:
            out = f.run(publications=[pub(lastmod)])
        assert out["to_process"] == [] and [p.publication_id for p in out["unchanged"]] == [SLO_ID]
        assert out["skipped"] == 1 and f._fetcher.calls == []
        assert out["unchanged"][0].sitemap_lastmod == "2025-12-01T15:25Z"  # date enregistree, pas celle du sitemap
        assert run.counts["classify:unchanged_sitemap"] == 1


@pytest.mark.parametrize("ref_date, stored_date, kw", [
    ("2025-12-02T00:00Z", "2025-12-01T15:25Z", {}),                     # plus recente
    (None, "2025-12-01T15:25Z", {}),                                    # pas de date dans le sitemap
    ("2025-12-01T15:25Z", None, {}),                                    # pas de date dans l'etat
    ("2025-12-01T15:25Z", "2025-12-01T15:25Z", {"full": True}),
    ("2025-12-01T15:25Z", "2025-12-01T15:25Z", {"recheck_all": True}),
])
def test_newer_or_missing_date_full_and_recheck_all_fetch_the_page(tmp_path, ref_date, stored_date, kw):
    known(tmp_path, stored_date)
    f = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html")})
    out = f.run(publications=[pub(ref_date)], **kw)
    assert f._fetcher.calls == [SLO_PUB] and out["skipped"] == 0
    assert out["undated"] == (1 if ref_date is None else 0)


def test_recheck_days_still_protect_a_resumed_recheck(tmp_path):
    known(tmp_path, "2025-12-01T15:25Z", processed=date.today().isoformat())
    f = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html")})
    assert f.run(publications=[pub("2025-12-01T15:25Z")], recheck_all=True)["unchanged"] and f._fetcher.calls == []


def test_skipped_publications_do_not_count_against_the_limit(tmp_path):
    known(tmp_path, "2025-12-01T15:25Z")
    other = Publication(publication_id="three", url="https://reforms-investments.ec.europa.eu/publications-0/three_en",
                        project_ids=["p"])
    f = fetcher_component(tmp_path, {other.url: fixture_bytes("pub_three_files.html")})
    out = f.run(publications=[pub("2025-12-01T15:25Z"), other], limit=1)
    assert out["skipped"] == 1 and [p.publication_id for p in out["to_process"]] == ["three"]
    assert out["remaining"] == 0


# --- writer : enregistrement de la date -----------------------------------------------------------------------------

def test_writer_records_the_date_of_fetched_unchanged_publications_only_when_given(tmp_path):
    known(tmp_path, "2025-01-01T00:00Z")
    w = DeliverableWriter(output_dir=str(tmp_path))
    w._store = LocalProjectStore(tmp_path)
    w.run(records=[], unchanged=[pub("2025-06-01T00:00Z")])
    assert DeliverableStore(tmp_path).get(SLO_ID).sitemap_lastmod == "2025-06-01T00:00Z"
    w.run(records=[], unchanged=[pub(None)])  # run sans sitemap : la date connue reste
    assert DeliverableStore(tmp_path).get(SLO_ID).sitemap_lastmod == "2025-06-01T00:00Z"


# --- de bout en bout : runner, echecs, CLI ------------------------------------------------------------------------

def wired(settings, gen, tmp_path, dates, pages=None, missing=()):
    p = build_deliverables_pipeline(settings, chat_generator=gen, sitemap_url=SITEMAP)
    store = project_store(tmp_path)
    project_file(store, "slo")
    site = {SITEMAP: urlset(dates), SLO_PUB: fixture_bytes("pub_one_file.html"),
            PDF: fixture_bytes("deliverable_small.pdf"), **(pages or {})}
    fake = FakeFetcher(pages={k: v for k, v in site.items() if k not in missing},
                       cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html")})
    for name in ("discoverer", "publications", "texts"):
        p.get_component(name)._fetcher = fake
    p.get_component("discoverer")._store = store
    p.get_component("writer")._store = store
    return p, fake


def age_state(tmp_path):
    ds = DeliverableStore(tmp_path)
    ds.put(ds.get(SLO_ID).model_copy(update={"processed": OLD}))  # au-dela de recheck_days
    ds.save()


def test_run_deliverables_records_dates_then_skips_and_failures_keep_the_old_date(tmp_path):
    s = pipeline_settings(tmp_path)
    dates = {SLO_PUB: "2025-12-01T15:25Z"}
    built = {}

    def factory(gen, **site):
        def make(settings, **kw):
            assert kw.get("sitemap_url") == SITEMAP
            p, built["fetcher"] = wired(settings, gen, tmp_path, **site)
            return p
        return make

    first = run_deliverables(s, run_id="d1", sitemap_url=SITEMAP,
                             pipeline_factory=factory(FakeChatGenerator(replies=[CONTENT]), dates=dates))
    assert first["written"]["publications"] == 1 and first["skipped_by_sitemap"] == 0
    assert DeliverableStore(tmp_path).get(SLO_ID).sitemap_lastmod == "2025-12-01T15:25Z"

    age_state(tmp_path)
    second = run_deliverables(s, run_id="d2", sitemap_url=SITEMAP,
                              pipeline_factory=factory(FakeChatGenerator(replies=[CONTENT]), dates=dates))
    assert second["skipped_by_sitemap"] == 1 and SLO_PUB not in built["fetcher"].calls
    assert second["publication_without_sitemap_date"] == 0

    # Date plus recente, page de publication en echec : date inchangee.
    newer = {SLO_PUB: "2026-01-01T00:00Z"}
    run_deliverables(s, run_id="d3", sitemap_url=SITEMAP,
                     pipeline_factory=factory(FakeChatGenerator(replies=[CONTENT]), dates=newer, missing={SLO_PUB}))
    assert DeliverableStore(tmp_path).get(SLO_ID).sitemap_lastmod == "2025-12-01T15:25Z"

    # Date plus recente, fichiers changes, resume en echec : date inchangee.
    changed = fixture_bytes("pub_one_file.html").replace(b"Final%20-%20deliverable.pdf", b"Final%20-%20v2.pdf")
    assert changed != fixture_bytes("pub_one_file.html")
    v2 = PDF.replace("Final%20-%20deliverable.pdf", "Final%20-%20v2.pdf")
    run_deliverables(s, run_id="d4", sitemap_url=SITEMAP,
                     pipeline_factory=factory(FakeChatGenerator(error=RuntimeError("boom")), dates=newer,
                                              pages={SLO_PUB: changed, v2: fixture_bytes("deliverable_small.pdf")}))
    assert DeliverableStore(tmp_path).get(SLO_ID).sitemap_lastmod == "2025-12-01T15:25Z"

    # Date plus recente, page relue et inchangee : la nouvelle date est enregistree.
    run_deliverables(s, run_id="d5", sitemap_url=SITEMAP,
                     pipeline_factory=factory(FakeChatGenerator(replies=[CONTENT]), dates=newer))
    assert DeliverableStore(tmp_path).get(SLO_ID).sitemap_lastmod == "2026-01-01T00:00Z"


def test_cli_deliverables_passes_recheck_all_and_the_sitemap(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_check_llm", lambda settings: True)
    monkeypatch.setattr(cli, "run_deliverables", lambda *a, **k: seen.update(k) or {"quota_exceeded": False})
    assert cli.main(["deliverables", "--recheck-all"]) == 0
    assert seen["recheck_all"] is True and seen["sitemap_url"] == SITEMAP
    assert cli.main(["deliverables"]) == 0 and seen["recheck_all"] is False
