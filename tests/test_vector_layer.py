import shutil

import pytest
from haystack import Document
from haystack.components.embedders import MockDocumentEmbedder
from haystack.dataclasses import ByteStream

from conftest import FIXTURES
from tsi_agent import runlog
from tsi_agent.chunking import CHUNKER_VERSION
from tsi_agent.components.chunker import ProjectChunker
from tsi_agent.components.file_loader import ProjectFileLoader
from tsi_agent.components.vector_writer import VectorLayerWriter
from tsi_agent.vector_state import (FileState, VectorLayerError, VectorState, load_state, load_store, new_store,
                                    save_state, save_store, store_path)


def stream(name: str, rel: str) -> ByteStream:
    return ByteStream(data=(FIXTURES / name).read_bytes(), meta={"file_path": rel, "content_hash": "sha256:x"})


def test_chunker_chunks_valid_files_and_skips_invalid(tmp_path):
    bad = ByteStream(data=b"no front matter", meta={"file_path": "projects/bad.md"})
    with runlog.open_run(tmp_path, "r1") as run:
        out = ProjectChunker().run(sources=[stream("vec_thin.md", "projects/thin.md"), bad])
    assert [d.meta["source_file"] for d in out["documents"]] == ["projects/thin.md"]
    assert out["skipped"] == ["projects/bad.md"]
    assert run.final_summary["counts"]["chunk:ok"] == 1 and run.final_summary["alerts"]


def test_chunker_accepts_deepset_file_name_meta():
    src = ByteStream(data=(FIXTURES / "vec_thin.md").read_bytes(), meta={"file_name": "thin.md"})
    assert ProjectChunker().run(sources=[src])["documents"][0].meta["source_file"] == "thin.md"


def make_output(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "programme").mkdir()
    shutil.copy(FIXTURES / "vec_thin.md", out / "projects" / "thin.md")
    shutil.copy(FIXTURES / "vec_detail_sections.md", out / "projects" / "detail.md")
    shutil.copy(FIXTURES / "vec_programme.md", out / "programme" / "country-croatia.md")
    return out


def loader(out, embedder_id="mock:m"):
    return ProjectFileLoader(output_dir=str(out), vectors_dir=str(out / "vectors"), embedder_id=embedder_id)


def test_state_and_store_round_trip(tmp_path):
    state = VectorState(embedder_id="e", chunker_version=1, files={"a.md": FileState(hash="h", chunk_ids=["a#0"])})
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    store = new_store()
    store.write_documents([Document(id="a#0", content="x", embedding=[0.1, 0.2], meta={"k": 1})])
    save_store(store, store_path(tmp_path))
    loaded = load_store(store_path(tmp_path))
    assert [d.id for d in loaded.filter_documents()] == ["a#0"]
    assert loaded.filter_documents()[0].embedding == [0.1, 0.2]


def test_unreadable_store_is_a_clear_error(tmp_path):
    store_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(VectorLayerError, match="--full"):
        load_store(store_path(tmp_path))


def test_loader_first_run_is_full(tmp_path):
    out = make_output(tmp_path)
    res = loader(out).run()
    assert res["full"] is True and res["removed"] == []
    assert sorted(s.meta["file_path"] for s in res["sources"]) == [
        "programme/country-croatia.md", "projects/detail.md", "projects/thin.md"]
    assert all(s.meta["content_hash"].startswith("sha256:") for s in res["sources"])


def indexed_state(out, embedder_id="mock:m", version=CHUNKER_VERSION):
    """Etat tel qu'apres un run complet : tous les fichiers actuels indexes."""
    files = {}
    for s in loader(out).run(full=True)["sources"]:
        files[s.meta["file_path"]] = FileState(hash=s.meta["content_hash"], chunk_ids=[s.meta["file_path"] + "#0"])
    vdir = out / "vectors"
    save_state(vdir, VectorState(embedder_id=embedder_id, chunker_version=version, files=files))
    save_store(new_store(), store_path(vdir))


def test_loader_incremental_new_changed_removed(tmp_path):
    out = make_output(tmp_path)
    indexed_state(out)
    assert loader(out).run() == {"sources": [], "removed": [], "full": False}
    (out / "projects" / "detail.md").write_bytes((out / "projects" / "detail.md").read_bytes() + b"\nmore\n")
    shutil.copy(FIXTURES / "vec_detail_page_text.md", out / "projects" / "new.md")
    (out / "programme" / "country-croatia.md").unlink()
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        res = loader(out).run()
    assert res["full"] is False
    assert sorted(s.meta["file_path"] for s in res["sources"]) == ["projects/detail.md", "projects/new.md"]
    assert res["removed"] == ["programme/country-croatia.md"]
    counts = run.final_summary["counts"]
    assert counts["load:new"] == 1 and counts["load:changed"] == 1 and counts["load:removed"] == 1


@pytest.mark.parametrize("change", ["embedder", "chunker", "store", "flag"])
def test_loader_forces_full(tmp_path, change):
    out = make_output(tmp_path)
    indexed_state(out, version=CHUNKER_VERSION - 1 if change == "chunker" else CHUNKER_VERSION)
    if change == "store":
        store_path(out / "vectors").unlink()
    res = loader(out, embedder_id="other:m" if change == "embedder" else "mock:m").run(full=change == "flag")
    assert res["full"] is True and len(res["sources"]) == 3 and res["removed"] == []


def index_once(out, *, full=False, embedder_id="MockDocumentEmbedder:mock", pricing=None):
    """Un passage complet loader -> chunker -> mock embedder -> writer, sans Pipeline."""
    loaded = ProjectFileLoader(output_dir=str(out), vectors_dir=str(out / "vectors"),
                               embedder_id=embedder_id).run(full=full)
    chunked = ProjectChunker().run(sources=loaded["sources"])
    embedded = MockDocumentEmbedder(model="mock", dimension=8).run(documents=chunked["documents"])
    writer = VectorLayerWriter(vectors_dir=str(out / "vectors"), embedder_id=embedder_id, pricing=pricing)
    return writer.run(documents=embedded["documents"], sources=loaded["sources"], removed=loaded["removed"],
                      full=loaded["full"], skipped=chunked["skipped"], embed_meta=embedded["meta"])["stats"]


def stored_ids(out):
    return sorted(d.id for d in load_store(store_path(out / "vectors")).filter_documents())


def test_writer_full_then_incremental(tmp_path):
    out = make_output(tmp_path)
    stats = index_once(out)
    assert stats["files_indexed"] == 3 and stats["chunks_deleted"] == 0
    total = stats["chunks_total"]
    assert total == len(stored_ids(out)) and "projects/detail#0" in stored_ids(out)
    state = load_state(out / "vectors")
    assert state.embedder_id == "MockDocumentEmbedder:mock" and state.chunker_version == CHUNKER_VERSION
    assert state.files["projects/thin.md"].chunk_ids == ["projects/thin#0"]
    loaded_docs = load_store(store_path(out / "vectors")).filter_documents()
    assert loaded_docs and all(d.meta["embedder"] == "MockDocumentEmbedder:mock" for d in loaded_docs)

    assert index_once(out) == {"files_indexed": 0, "files_removed": 0, "chunks_written": 0, "chunks_deleted": 0,
                               "chunks_total": total}

    (out / "projects" / "thin.md").unlink()
    stats = index_once(out)
    assert stats["files_removed"] == 1 and stats["chunks_deleted"] == 1 and stats["chunks_total"] == total - 1
    assert "projects/thin.md" not in load_state(out / "vectors").files


def test_writer_changed_file_replaces_its_chunks(tmp_path):
    out = make_output(tmp_path)
    index_once(out)
    before = stored_ids(out)
    detail = out / "projects" / "detail.md"
    detail.write_text(detail.read_text(encoding="utf-8").replace("## Context", "## Context\n\nNew first line."),
                      encoding="utf-8")
    stats = index_once(out)
    assert stats["files_indexed"] == 1 and stats["chunks_deleted"] == stats["chunks_written"]
    assert stored_ids(out) == before
    ctx = [d for d in load_store(store_path(out / "vectors")).filter_documents()
           if d.meta["section"] == "Context"][0]
    assert ctx.content.startswith("New first line.")


def test_writer_skipped_file_is_retried_next_run(tmp_path):
    out = make_output(tmp_path)
    index_once(out)
    (out / "projects" / "thin.md").write_text("broken", encoding="utf-8")
    index_once(out)
    assert "projects/thin.md" not in load_state(out / "vectors").files
    assert not any(i.startswith("projects/thin#") for i in stored_ids(out))
    shutil.copy(FIXTURES / "vec_thin.md", out / "projects" / "thin.md")
    assert index_once(out)["files_indexed"] == 1


def test_writer_full_rebuilds_the_store(tmp_path):
    out = make_output(tmp_path)
    total = index_once(out)["chunks_total"]
    stats = index_once(out, embedder_id="MockDocumentEmbedder:other")
    assert stats["files_indexed"] == 3 and stats["chunks_total"] == total
    assert load_state(out / "vectors").embedder_id == "MockDocumentEmbedder:other"


def test_writer_logs_embedding_tokens_and_cost(tmp_path):
    out = make_output(tmp_path)
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        index_once(out, pricing={"mock": {"input": 1_000_000.0}})
    s = run.final_summary
    assert s["counts"]["embed:ok"] == 1 and s["tokens"]["input_tokens"] > 0
    assert s["cost_usd"] == pytest.approx(s["tokens"]["input_tokens"], rel=1e-6)
    assert s["counts"]["write:ok"] == 1


def test_writer_estimates_tokens_without_usage(tmp_path):
    docs = [Document(id="a#0", content="one two three", meta={"source_file": "a.md", "context_header": "T"})]
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        VectorLayerWriter(vectors_dir=str(tmp_path / "v"), embedder_id="x").run(
            documents=docs, sources=[ByteStream(data=b"", meta={"file_path": "a.md", "content_hash": "h"})],
            removed=[], full=True)
    assert run.final_summary["tokens"]["input_tokens"] == round(4 * 1.33)


def test_two_files_with_same_project_id_dont_collide(tmp_path):
    """Meme project_id (front matter duplique) a deux chemins differents : pas de collision d'id, pas d'ecrasement."""
    out = make_output(tmp_path)
    shutil.copy(out / "projects" / "detail.md", out / "projects" / "detail2.md")
    stats = index_once(out, full=True)
    assert stats["files_indexed"] == 4
    ids = stored_ids(out)
    assert any(i.startswith("projects/detail#") for i in ids)
    assert any(i.startswith("projects/detail2#") for i in ids)

    (out / "projects" / "detail2.md").unlink()
    index_once(out)
    ids = stored_ids(out)
    assert any(i.startswith("projects/detail#") for i in ids)
    assert not any(i.startswith("projects/detail2#") for i in ids)


def test_writer_recovers_from_interruption_before_state_save(tmp_path, monkeypatch):
    """Panne entre save_store et save_state (spec 3.4) : le run suivant reconcilie sans doublon ni orphelin."""
    import tsi_agent.components.vector_writer as vector_writer_module

    out = make_output(tmp_path)
    index_once(out)  # etat initial complet

    # Retire la section "## Expected results" de detail.md : son nombre de morceaux passe de 5 a 4.
    detail = out / "projects" / "detail.md"
    text = detail.read_text(encoding="utf-8")
    before, _, after = text.partition("## Expected results\n")
    _, _, rest = after.partition("## Sources")
    detail.write_text(before + "## Sources" + rest, encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash after save_store")

    monkeypatch.setattr(vector_writer_module, "save_state", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        index_once(out)
    monkeypatch.undo()  # store.json a ete sauvegarde avec les 4 nouveaux morceaux ; state.json est reste l'ancien

    # Run normal suivant : doit reconcilier, sans doublon ni morceau orphelin.
    index_once(out)
    ids_after_recovery = stored_ids(out)
    state = load_state(out / "vectors")
    expected_ids = sorted(cid for fs in state.files.values() for cid in fs.chunk_ids)
    assert ids_after_recovery == expected_ids

    # Doit correspondre a un full index frais sur le meme contenu.
    shutil.rmtree(out / "vectors")
    index_once(out, full=True)
    assert stored_ids(out) == ids_after_recovery


def test_unreadable_state_is_a_clear_error(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(VectorLayerError, match="--full"):
        load_state(tmp_path)


def test_loader_full_ignores_corrupted_state(tmp_path):
    """Avec --full, l'ancien etat est inutile : un state.json corrompu ne doit pas bloquer le run."""
    out = make_output(tmp_path)
    (out / "vectors").mkdir(parents=True)
    (out / "vectors" / "state.json").write_text("{not json", encoding="utf-8")
    res = loader(out).run(full=True)
    assert res["full"] is True and len(res["sources"]) == 3 and res["removed"] == []
