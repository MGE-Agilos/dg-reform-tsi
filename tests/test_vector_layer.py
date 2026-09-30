from haystack.dataclasses import ByteStream

from conftest import FIXTURES
from tsi_agent import runlog
from tsi_agent.components.chunker import ProjectChunker


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
