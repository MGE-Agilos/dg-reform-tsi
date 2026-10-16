import json
import subprocess
import zipfile
from datetime import date

import pytest

from tsi_agent import release

MANIFEST = {"corpus": "dg-reform-tsi", "files": 2, "files_by_type": {"project": 1, "csr": 1}, "chunks": 7,
            "chunks_by_type": {"project": 4, "csr": 3}, "largest_file": {"name": "tsi-project-a.md", "mb": 0.01},
            "files_over_10mb": False, "meta_type_problems": [], "skipped": []}


@pytest.fixture
def tree(tmp_path):
    out = tmp_path / "output"
    for rel in ("projects/a.md", "state/index.json", "csr/csr-FR-2023-1.md", "deliverables/x/text.md",
                "vectors/collection/data.bin", "reports/anomalies.md", "state/index.json.tmp",
                "deliverables/big.pdf.part", "state/supabase_rows.json"):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel, encoding="utf-8")
    build = tmp_path / "build"
    files = build / "files"
    files.mkdir(parents=True)
    for name in ("tsi-project-a.md", "tsi-project-a.md.meta.json", "tsi-csr-FR-2023-1.md",
                 "tsi-csr-FR-2023-1.md.meta.json", "notes.txt"):
        (files / name).write_text(name, encoding="utf-8")
    (build / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    return out, build, tmp_path / "release"


def names(path):
    with zipfile.ZipFile(path) as z:
        return sorted(z.namelist())


def test_build_release_writes_the_three_assets_and_notes(tree):
    out, build, root = tree
    summary = release.build_release(out, build, root, "data-2026-10-14", today=date(2026, 10, 14))
    rel = root / "data-2026-10-14"
    assert summary["dir"] == str(rel)
    assert set(summary["assets"]) == {"tsi-prod-files.zip", "manifest.json", "tsi-output-state.zip"}
    assert all(a["bytes"] > 0 for a in summary["assets"].values())
    assert names(rel / "tsi-prod-files.zip") == ["tsi-csr-FR-2023-1.md", "tsi-csr-FR-2023-1.md.meta.json",
                                                  "tsi-project-a.md", "tsi-project-a.md.meta.json"]
    assert names(rel / "tsi-output-state.zip") == ["csr/csr-FR-2023-1.md", "deliverables/x/text.md",
                                                    "projects/a.md", "state/index.json"]
    with zipfile.ZipFile(rel / "tsi-output-state.zip") as z:
        assert z.read("projects/a.md").decode() == "projects/a.md"
    assert json.loads((rel / "manifest.json").read_text(encoding="utf-8")) == MANIFEST
    notes = (rel / "NOTES.md").read_text(encoding="utf-8")
    assert "2026-10-14" in notes and "deploy/prod/GUIDE.md" in notes
    assert notes.startswith("# TSI data 2026-10-14") and "chunks expected" in notes
    assert "project" in notes and "csr" in notes and "2" in notes and "7" in notes
    assert summary["prod_files"] == 4 and summary["state_files"] == 4


def test_rebuild_replaces_previous_assets(tree):
    out, build, root = tree
    release.build_release(out, build, root, "t1")
    (out / "projects" / "a.md").unlink()
    release.build_release(out, build, root, "t1")
    assert "projects/a.md" not in names(root / "t1" / "tsi-output-state.zip")


def test_missing_manifest_is_an_error(tree):
    out, build, root = tree
    (build / "manifest.json").unlink()
    with pytest.raises(release.ReleaseError, match="prod-export"):
        release.build_release(out, build, root, "t")


def test_meta_type_problems_are_an_error(tree):
    out, build, root = tree
    (build / "manifest.json").write_text(json.dumps({**MANIFEST, "meta_type_problems": ["year: str/int"]}),
                                         encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="meta_type_problems"):
        release.build_release(out, build, root, "t")


def test_file_count_mismatch_is_an_error(tree):
    out, build, root = tree
    (build / "files" / "tsi-project-b.md").write_text("b", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="3"):
        release.build_release(out, build, root, "t")
    assert not (root / "t").exists()


class FakeRun:
    def __init__(self, view_code=1, create_code=0):
        self.view_code = view_code
        self.create_code = create_code
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        code = self.view_code if cmd[:3] == ["gh", "release", "view"] else self.create_code
        return subprocess.CompletedProcess(cmd, code, stdout="https://github.com/o/r/releases/tag/x\n",
                                           stderr="boom" if code else "")


def test_publish_calls_gh_release_create(tree, monkeypatch):
    out, build, root = tree
    release.build_release(out, build, root, "data-2026-10-14", today=date(2026, 10, 14))
    fake = FakeRun()
    monkeypatch.setattr(release.subprocess, "run", fake)
    rel = root / "data-2026-10-14"
    result = release.publish("data-2026-10-14", rel, repo="MGE-Agilos/dg-reform-tsi")
    view, create = fake.calls
    assert view == ["gh", "release", "view", "data-2026-10-14", "-R", "MGE-Agilos/dg-reform-tsi"]
    assert create == ["gh", "release", "create", "data-2026-10-14", str(rel / "tsi-prod-files.zip"),
                      str(rel / "manifest.json"), str(rel / "tsi-output-state.zip"),
                      "--title", "TSI data 2026-10-14", "--notes-file", str(rel / "NOTES.md"),
                      "--target", "main", "-R", "MGE-Agilos/dg-reform-tsi"]
    assert result["url"] == "https://github.com/o/r/releases/tag/x"


def test_publish_refuses_an_existing_tag(tree, monkeypatch):
    out, build, root = tree
    release.build_release(out, build, root, "t")
    fake = FakeRun(view_code=0)
    monkeypatch.setattr(release.subprocess, "run", fake)
    with pytest.raises(release.ReleaseError, match="existe"):
        release.publish("t", root / "t")
    assert len(fake.calls) == 1 and fake.calls[0] == ["gh", "release", "view", "t"]


def test_publish_reports_gh_failures(tree, monkeypatch):
    out, build, root = tree
    release.build_release(out, build, root, "t")
    monkeypatch.setattr(release.subprocess, "run", FakeRun(create_code=1))
    with pytest.raises(release.ReleaseError, match="boom"):
        release.publish("t", root / "t")

    def missing(cmd, **kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(release.subprocess, "run", missing)
    with pytest.raises(release.ReleaseError, match="gh"):
        release.publish("t", root / "t")


def test_publish_requires_built_assets(tmp_path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(release.subprocess, "run", fake)
    with pytest.raises(release.ReleaseError, match="release-data"):
        release.publish("t", tmp_path / "nothing")
    assert fake.calls == []


def test_cli_release_data(tree, tmp_path, monkeypatch, capsys):
    import tsi_agent.__main__ as cli

    out, build, root = tree
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(f"output_dir: {out.as_posix()}\n", encoding="utf-8")
    base = ["--settings", str(settings_file), "release-data", "--build", str(build), "--out", str(root)]
    assert cli.main([*base, "--tag", "data-x"]) == 0
    assert (root / "data-x" / "tsi-prod-files.zip").exists()
    fake = FakeRun()
    monkeypatch.setattr(release.subprocess, "run", fake)
    assert cli.main([*base, "--tag", "data-x", "--publish"]) == 0
    assert fake.calls[-1][:4] == ["gh", "release", "create", "data-x"]
    assert "-R" in fake.calls[-1] and release.DEFAULT_REPO in fake.calls[-1]
    (build / "manifest.json").unlink()
    assert cli.main([*base, "--tag", "data-y"]) == 1
    assert "prod-export" in capsys.readouterr().err


@pytest.mark.parametrize("tag", ["", "data 2026", "../x", "a;b", "--draft", "-x"])
def test_invalid_tags_are_refused(tree, tag, monkeypatch):
    out, build, root = tree
    with pytest.raises(release.ReleaseError, match="tag"):
        release.build_release(out, build, root, tag)
    fake = FakeRun()
    monkeypatch.setattr(release.subprocess, "run", fake)
    with pytest.raises(release.ReleaseError, match="tag"):
        release.publish(tag, root / "whatever")
    assert fake.calls == []


def test_files_over_10mb_are_an_error(tree):
    out, build, root = tree
    (build / "manifest.json").write_text(json.dumps({**MANIFEST, "files_over_10mb": True}), encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="10"):
        release.build_release(out, build, root, "t")


def test_failed_zip_leaves_no_part_file(tree, monkeypatch):
    out, build, root = tree

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(release.zipfile.ZipFile, "write", boom)
    with pytest.raises(OSError, match="disk full"):
        release.build_release(out, build, root, "t")
    assert list((root / "t").glob("*.part")) == []


def test_default_tag():
    assert release.default_tag(date(2026, 10, 14)) == "data-2026-10-14"


def test_state_zip_for_a_failed_ci_run(tree, tmp_path):
    out, _, _ = tree
    target = tmp_path / "partial" / "tsi-output-partial.zip"
    assert release.write_state_zip(out, target) == 4
    assert names(target) == ["csr/csr-FR-2023-1.md", "deliverables/x/text.md", "projects/a.md", "state/index.json"]
