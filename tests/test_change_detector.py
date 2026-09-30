from tsi_agent.components.change_detector import ChangeDetector
from tsi_agent.models import ProjectRef
from tsi_agent.store import LocalProjectStore, StoreEntry

SCOPE = {"countries": ["FR"], "srsp": False, "all_countries": False}


def thin(pid, country="FR", **kw):
    return ProjectRef(project_id=pid, title=pid, programme="TSI", year=2025, countries=[country], **kw)


def detail(pid, country="FR"):
    return ProjectRef(project_id=pid, title=pid, programme="TSI", year=2025, countries=[country],
                      detail_url=f"https://x/{pid}_en")


def known(store, ref, **kw):
    store.put(StoreEntry(project_id=ref.project_id, file="f", programme=ref.programme, countries=ref.countries,
                         listing_hash=ref.listing_hash(), first_seen="d", last_updated="d", **kw))


def detector(tmp_path):
    d = ChangeDetector(output_dir=str(tmp_path))
    d._store = LocalProjectStore(tmp_path)
    return d


def test_new_changed_and_unchanged_thin(tmp_path):
    d = detector(tmp_path)
    same, changed = thin("same"), thin("changed")
    known(d._store, same)
    known(d._store, changed)
    out = d.run(refs=[same, changed.model_copy(update={"highlighted": True}), thin("new")], run_id="r", scope=SCOPE)
    assert [r.project_id for r in out["thin"]] == ["changed", "new"]
    assert out["detail"] == [] and out["delisted"] == []


def test_detail_limit_and_offered_once_per_run(tmp_path):
    d = detector(tmp_path)
    refs = [detail(f"p{i}") for i in range(5)]
    first = d.run(refs=refs, run_id="r", scope=SCOPE, limit=2)
    assert [r.project_id for r in first["detail"]] == ["p0", "p1"] and first["remaining"] == 3
    second = d.run(refs=refs, run_id="r", scope=SCOPE, limit=2)
    assert [r.project_id for r in second["detail"]] == ["p2", "p3"] and second["remaining"] == 1
    other_run = d.run(refs=refs, run_id="r2", scope=SCOPE)
    assert len(other_run["detail"]) == 5


def test_thin_already_covered_by_detail_alias_is_skipped(tmp_path):
    d = detector(tmp_path)
    known(d._store, detail("academy"), aliases=["tsi-2025-academy"])
    out = d.run(refs=[detail("academy").model_copy(update={"aliases": ["tsi-2025-academy"]}), thin("tsi-2025-academy")], run_id="r", scope=SCOPE)
    assert out["thin"] == []


def test_multi_country_entry_needs_all_its_countries_in_scope(tmp_path):
    d = detector(tmp_path)
    known(d._store, thin("frde").model_copy(update={"countries": ["FR", "DE"]}))
    assert d.run(refs=[], run_id="r", scope=SCOPE)["delisted"] == []
    both = {"countries": ["FR", "DE"], "srsp": False, "all_countries": False}
    assert d.run(refs=[], run_id="r2", scope=both)["delisted"] == ["frde"]


def test_srsp_and_countryless_entries_scope(tmp_path):
    d = detector(tmp_path)
    known(d._store, ProjectRef(project_id="srsp-2019-x", title="x", programme="SRSP", year=2019, countries=["AT"]))
    known(d._store, ProjectRef(project_id="eu-wide", title="e", detail_url="https://x/e_en"))
    assert d.run(refs=[], run_id="r", scope=SCOPE)["delisted"] == []
    full = {"countries": ["FR"], "srsp": True, "all_countries": True}
    assert sorted(d.run(refs=[], run_id="r2", scope=full)["delisted"]) == ["eu-wide", "srsp-2019-x"]


def test_thin_comes_back_when_its_detail_page_vanished(tmp_path):
    d = detector(tmp_path)
    known(d._store, detail("academy"), aliases=["tsi-2025-academy"])
    out = d.run(refs=[thin("tsi-2025-academy")], run_id="r", scope=SCOPE)
    assert [r.project_id for r in out["thin"]] == ["tsi-2025-academy"]
    assert out["delisted"] == ["academy"]


def test_delist_only_in_scope_and_not_for_suspect_countries(tmp_path):
    d = detector(tmp_path)
    for i in range(6):
        known(d._store, thin(f"fr{i}"))
    known(d._store, thin("de0", country="DE"))
    known(d._store, thin("gone"))
    refs = [thin(f"fr{i}") for i in range(6)]
    out = d.run(refs=refs, run_id="r", scope=SCOPE)
    assert out["delisted"] == ["gone"]  # DE hors périmètre
    out = d.run(refs=refs, run_id="r", scope=SCOPE, suspect_countries=["FR"])
    assert out["delisted"] == []


def test_big_drop_makes_country_suspect(tmp_path):
    d = detector(tmp_path)
    for i in range(10):
        known(d._store, thin(f"fr{i}"))
    out = d.run(refs=[thin("fr0"), thin("fr1")], run_id="r", scope=SCOPE)
    assert out["delisted"] == []


def test_deferred_ids_are_not_delisted(tmp_path):
    d = detector(tmp_path)
    known(d._store, thin("busy"))
    out = d.run(refs=[], run_id="r", scope=SCOPE, deferred_ids=["busy"])
    assert out["delisted"] == []
