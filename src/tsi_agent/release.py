"""`release-data` : prepare les donnees de production pour une release GitHub, et la publie avec `gh`.

Fichiers joints a la release (dossier `deploy/release/<tag>/`) :
- `tsi-prod-files.zip` : les fichiers `tsi-*` de `deploy/prod/build/files` (avec leurs `.meta.json`), a plat ;
- `manifest.json` : le manifeste de `prod-export` ;
- `tsi-output-state.zip` : le contenu de `output/` (chemins relatifs a `output/`), sauf `vectors/`, `reports/` et les
  fichiers temporaires (`*.tmp`, `*.part`) ; c'est l'etat a restaurer pour une mise a jour sur un autre PC.
`NOTES.md` sert de description de la release.
"""

import json
import re
import shutil
import subprocess
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_REPO = "MGE-Agilos/dg-reform-tsi"
PROD_ZIP = "tsi-prod-files.zip"
MANIFEST = "manifest.json"
STATE_ZIP = "tsi-output-state.zip"
NOTES = "NOTES.md"
ASSETS = (PROD_ZIP, MANIFEST, STATE_ZIP)
EXCLUDED_DIRS = ("vectors", "reports")
EXCLUDED_SUFFIXES = (".tmp", ".part")
GUIDE_URL = f"https://github.com/{DEFAULT_REPO}/blob/main/deploy/prod/GUIDE.md"
_DATED_TAG = re.compile(r"^data-(\d{4}-\d{2}-\d{2})$")
_VALID_TAG = re.compile(r"^[A-Za-z0-9._-]+$")
TARGET_BRANCH = "main"


class ReleaseError(RuntimeError):
    pass


def default_tag(today: date | None = None) -> str:
    return f"data-{(today or date.today()).isoformat()}"


def _check_tag(tag: str) -> None:
    if not _VALID_TAG.match(tag or "") or tag.startswith("-"):
        raise ReleaseError(f"tag invalide : {tag!r} (lettres, chiffres, '.', '_' et '-' seulement, "
                           "ex. data-2026-10-14)")


def _check_manifest(prod_build_dir: Path) -> tuple[dict[str, Any], list[Path]]:
    path = prod_build_dir / MANIFEST
    if not path.exists():
        raise ReleaseError(f"{path} introuvable : lancer d'abord `python -m tsi_agent prod-export`")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"{path} illisible ({exc}) : relancer `python -m tsi_agent prod-export`") from exc
    if manifest.get("meta_type_problems"):
        raise ReleaseError(f"meta_type_problems non vide dans {path} : {manifest['meta_type_problems']} "
                           "(corriger puis relancer prod-export)")
    if manifest.get("files_over_10mb"):
        raise ReleaseError(f"fichier de plus de 10 Mo dans l'export ({manifest.get('largest_file')}) : refuse par la "
                           "plateforme, corriger puis relancer prod-export")
    files = sorted(p for p in (prod_build_dir / "files").glob("tsi-*") if p.is_file())
    md = [p for p in files if p.name.endswith(".md")]
    if len(md) != manifest.get("files"):
        raise ReleaseError(f"{len(md)} fichiers tsi-*.md dans {prod_build_dir / 'files'}, mais le manifeste en "
                           f"annonce {manifest.get('files')} : relancer `python -m tsi_agent prod-export`")
    return manifest, files


def _state_files(output_dir: Path) -> list[tuple[Path, str]]:
    out = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(output_dir)
        if rel.parts[0] in EXCLUDED_DIRS or path.name.endswith(EXCLUDED_SUFFIXES):
            continue
        out.append((path, rel.as_posix()))
    return out


def _zip(target: Path, entries: list[tuple[Path, str]]) -> None:
    tmp = target.with_name(target.name + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for path, arcname in entries:
                z.write(path, arcname)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _notes(tag: str, day: date, manifest: dict[str, Any]) -> str:
    def by_type(key: str) -> str:
        return ", ".join(f"{k} {v}" for k, v in (manifest.get(key) or {}).items()) or "-"

    return f"""# Données TSI {day.isoformat()} (`{tag}`)

Corpus `{manifest.get('corpus', '-')}` : {manifest.get('files')} fichiers, {manifest.get('chunks')} morceaux attendus.

- Fichiers par type : {by_type('files_by_type')}
- Morceaux par type : {by_type('chunks_by_type')}

## Fichiers joints

- `{PROD_ZIP}` : fichiers à téléverser dans le workspace Haystack Enterprise (`tsi-*.md` et leurs `.meta.json`).
- `{MANIFEST}` : chiffres attendus après indexation (fichiers et morceaux par type).
- `{STATE_ZIP}` : état de `output/` (sans `vectors/` ni `reports/`), pour une mise à jour sur un autre PC
  (`deploy/updater/restore-state.ps1`).

## Procédure courte

1. Télécharger `{PROD_ZIP}` et `{MANIFEST}`.
2. Dans le workspace, supprimer les fichiers `tsi-*`, puis téléverser le contenu du zip.
3. Contrôler l'indexation avec les chiffres du manifeste, puis republier les pipelines.

Détail : deploy/prod/GUIDE.md ({GUIDE_URL}).
"""


def build_release(output_dir: Path, prod_build_dir: Path, out_root: Path, tag: str, *,
                  today: date | None = None) -> dict[str, Any]:
    """Prepare `out_root/<tag>/` (zips, manifeste, NOTES.md) ; erreur claire si l'export de prod est incoherent."""
    _check_tag(tag)
    output_dir, prod_build_dir = Path(output_dir), Path(prod_build_dir)
    manifest, prod_files = _check_manifest(prod_build_dir)
    if not output_dir.is_dir():
        raise ReleaseError(f"{output_dir} introuvable")
    rel_dir = Path(out_root) / tag
    rel_dir.mkdir(parents=True, exist_ok=True)
    _zip(rel_dir / PROD_ZIP, [(p, p.name) for p in prod_files])
    shutil.copyfile(prod_build_dir / MANIFEST, rel_dir / MANIFEST)
    state = _state_files(output_dir)
    _zip(rel_dir / STATE_ZIP, state)
    day = today or date.today()
    (rel_dir / NOTES).write_text(_notes(tag, day, manifest), encoding="utf-8")
    return {"tag": tag, "dir": str(rel_dir), "prod_files": len(prod_files), "state_files": len(state),
            "assets": {name: {"path": str(rel_dir / name), "bytes": (rel_dir / name).stat().st_size}
                       for name in ASSETS},
            "notes": str(rel_dir / NOTES)}


def _gh(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ReleaseError(f"impossible de lancer gh (GitHub CLI installé et `gh auth login` fait ?) : {exc}") from exc


def publish(tag: str, release_dir: Path, repo: str | None = None, *, title_date: str | None = None) -> dict[str, Any]:
    """`gh release create <tag> <fichiers> --title ... --notes-file NOTES.md` ; refuse un tag existant."""
    _check_tag(tag)
    release_dir = Path(release_dir)
    missing = [n for n in (*ASSETS, NOTES) if not (release_dir / n).exists()]
    if missing:
        raise ReleaseError(f"fichiers absents dans {release_dir} : {', '.join(missing)} "
                           "(lancer d'abord `python -m tsi_agent release-data`)")
    repo_args = ["-R", repo] if repo else []
    if _gh(["gh", "release", "view", tag, *repo_args]).returncode == 0:
        raise ReleaseError(f"la release {tag} existe deja sur GitHub : choisir un autre tag (--tag), "
                           "rien n'a ete ecrase")
    match = _DATED_TAG.match(tag)
    label = title_date or (match.group(1) if match else date.today().isoformat())
    cmd = ["gh", "release", "create", tag, *(str(release_dir / n) for n in ASSETS),
           "--title", f"Données TSI {label}", "--notes-file", str(release_dir / NOTES),
           "--target", TARGET_BRANCH, *repo_args]
    proc = _gh(cmd)
    if proc.returncode != 0:
        raise ReleaseError(f"gh release create a échoué (code {proc.returncode}) : "
                           f"{(proc.stderr or proc.stdout or '').strip()[:500]}")
    return {"tag": tag, "url": (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else None}
