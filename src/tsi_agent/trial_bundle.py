"""Corpus regroupe en quelques fichiers Markdown pour le trial Haystack Enterprise Platform.

Le plan d'essai (Studio) limite le workspace a 50 fichiers de 10 MB : les fichiers projets et programme sont
regroupes par pays (un fichier par pays, un pour les projets multi-pays, un pour le programme). Chaque morceau
de la couche vectorisee locale (chunking.chunk_file, memes regles) devient une section `# ...` : sur la
plateforme, MarkdownHeaderSplitter (niveau 1, sans second decoupage) retrouve exactement ces morceaux, et le
titre de section garde le contexte (titre, programme, annee, pays) dans le texte vectorise. Les metadonnees par
projet n'existant pas sur la plateforme (le `.meta.json` vaut pour tout le fichier), l'identifiant, le domaine,
le resultat et l'URL source sont ecrits en fin de section.
"""

import json
import re
from pathlib import Path

from haystack import Document

from tsi_agent.chunking import chunk_file
from tsi_agent.countries import COUNTRIES, name_from_iso

MAX_BYTES = 9_000_000  # marge sous la limite de 10 MB par fichier du plan d'essai
FOLDERS = ("projects", "programme")
MULTI = "multi-country"
PROGRAMME = "programme"
_HEADING = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)


def bundle_key(meta: dict) -> str:
    """Code ISO du pays pour un projet d'un seul pays, `multi-country` sinon, `programme` pour le programme."""
    if meta.get("doc_type") == "programme":
        return PROGRAMME
    countries = meta.get("countries") or []
    if len(countries) == 1 and countries[0] in COUNTRIES:
        return countries[0]
    return MULTI


def section(doc: Document) -> str:
    """Un morceau en section Markdown de niveau 1 ; les titres du texte sont neutralises (un seul niveau)."""
    meta = doc.meta
    heading = " — ".join(p for p in (meta.get("context_header"), meta.get("section")) if p)
    body = _HEADING.sub("", doc.content.strip())
    facts = [f"{'Project' if meta.get('doc_type') == 'project' else 'Document'} id: {meta.get('doc_id')}"]
    if meta.get("policy_area"):
        facts.append(f"Policy area: {meta['policy_area']}")
    if meta.get("results"):
        facts.append(f"Results: {meta['results']}")
    if meta.get("detail_page") is False:
        facts.append("Detail page: no")
    if meta.get("source_url"):
        facts.append(f"Source: {meta['source_url']}")
    note = "\n\n(AI-generated summary, not the official text.)" if meta.get("ai_generated") else ""
    return f"# {heading}\n\n{body}{note}\n\n{' · '.join(facts)}\n"


def collect(output_dir: Path, folders: tuple[str, ...] = FOLDERS) -> tuple[dict[str, list[str]], list[str]]:
    """Sections par regroupement, dans l'ordre des fichiers ; et les fichiers illisibles (ignores)."""
    bundles: dict[str, list[str]] = {}
    skipped: list[str] = []
    for folder in folders:
        for path in sorted((output_dir / folder).glob("*.md")):
            rel = f"{folder}/{path.name}"
            try:
                docs = chunk_file(rel, path.read_text(encoding="utf-8"))
            except ValueError:
                skipped.append(rel)
                continue
            for doc in docs:
                bundles.setdefault(bundle_key(doc.meta), []).append(section(doc))
    return bundles, skipped


def _label(key: str) -> str:
    return name_from_iso(key) if key in COUNTRIES else key


def _slug(key: str) -> str:
    return COUNTRIES[key][0] if key in COUNTRIES else key


def _parts(sections: list[str], max_bytes: int) -> list[list[str]]:
    parts: list[list[str]] = [[]]
    size = 0
    for text in sections:
        n = len(text.encode("utf-8")) + 1
        if parts[-1] and size + n > max_bytes:
            parts.append([])
            size = 0
        parts[-1].append(text)
        size += n
    return parts


def write_bundles(bundles: dict[str, list[str]], out_dir: Path, max_bytes: int = MAX_BYTES) -> list[Path]:
    """Ecrit `tsi-<pays>.md` (+ `-partN` au-dela de max_bytes) et leur `.meta.json` ; vide d'abord out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("tsi-*"):
        old.unlink()
    written: list[Path] = []
    for key in sorted(bundles, key=lambda k: (k in (MULTI, PROGRAMME), _slug(k))):
        parts = _parts(bundles[key], max_bytes)
        for i, sections in enumerate(parts, start=1):
            suffix = f"-part{i}" if len(parts) > 1 else ""
            path = out_dir / f"tsi-{_slug(key)}{suffix}.md"
            path.write_text("\n".join(sections), encoding="utf-8", newline="\n")
            meta = {"bundle": key, "country": _label(key), "chunks": len(sections)}
            Path(f"{path}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            written.append(path)
    return written


def build(output_dir: Path, out_dir: Path, max_bytes: int = MAX_BYTES) -> dict:
    """Regroupe et ecrit ; renvoie le bilan (fichiers, morceaux, taille max, fichiers ignores)."""
    bundles, skipped = collect(output_dir)
    files = write_bundles(bundles, out_dir, max_bytes)
    return {
        "out_dir": str(out_dir),
        "files": len(files),
        "chunks": sum(len(s) for s in bundles.values()),
        "largest_mb": round(max((f.stat().st_size for f in files), default=0) / 1_000_000, 2),
        "skipped": skipped,
    }
