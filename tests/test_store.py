from tsi_agent.store import LocalProjectStore, StoreEntry


def entry(pid, **kw):
    return StoreEntry(project_id=pid, file=f"projects/{pid}.md", first_seen="2026-09-30", last_updated="2026-09-30", **kw)


def test_put_get_persist_and_remove(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.put(entry("a", content_hash="sha256:1", countries=["FR"]))
    reloaded = LocalProjectStore(tmp_path)
    assert reloaded.get("a").content_hash == "sha256:1"
    assert list(reloaded.all()) == ["a"]
    reloaded.remove("a")
    assert LocalProjectStore(tmp_path).get("a") is None


def test_get_returns_a_copy(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.put(entry("a"))
    e = store.get("a")
    e.listed = False
    assert store.get("a").listed is True


def test_project_and_programme_files(tmp_path):
    store = LocalProjectStore(tmp_path)
    assert store.write_project_file("a", "hello") == "projects/a.md"
    assert store.read_project_file("a") == "hello"
    store.delete_project_file("a")
    assert store.read_project_file("a") is None
    assert store.write_programme_doc("tsi-performance", "text", "sha256:x") == "programme/tsi-performance.md"
    assert LocalProjectStore(tmp_path).programme_hash("tsi-performance") == "sha256:x"


def test_long_ids_get_a_bounded_file_name(tmp_path):
    from tsi_agent.store import MAX_FILE_STEM, file_stem
    long_id = "x" * 150
    stem = file_stem(long_id)
    assert len(stem) == MAX_FILE_STEM and stem != file_stem("x" * 151)
    assert file_stem("short") == "short"
    store = LocalProjectStore(tmp_path)
    assert store.write_project_file(long_id, "t") == f"projects/{stem}.md"
    assert store.read_project_file(long_id) == "t"


def test_put_without_save_is_persisted_by_flush(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.put(entry("a"), save=False)
    assert LocalProjectStore(tmp_path).get("a") is None
    store.flush()
    assert LocalProjectStore(tmp_path).get("a") is not None


def test_shared_store_is_one_instance_per_directory(tmp_path):
    from tsi_agent.store import shared_store
    a = shared_store(tmp_path / "out")
    assert shared_store(str(tmp_path / "out")) is a
    assert shared_store(tmp_path / "other") is not a


def test_index_save_retries_transient_permission_error(tmp_path, monkeypatch):
    import tsi_agent.store as store_mod
    real_replace = store_mod.os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("locked by OneDrive")
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", flaky)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    store = LocalProjectStore(tmp_path)
    store.put(entry("a"))
    assert len(calls) == 3
    monkeypatch.setattr(store_mod.os, "replace", real_replace)
    assert LocalProjectStore(tmp_path).get("a") is not None
