"""Corpus complet pour la production sur Haystack Enterprise Platform : un fichier par document.

Chaque fichier projet, programme ou livrable de output/ devient `tsi-<type>-<nom>.md` : ses morceaux de la couche
vectorisee locale (chunking.chunk_file, memes regles) en sections `# ...` (MarkdownHeaderSplitter niveau 1 sur la
plateforme), et `tsi-<type>-<nom>.md.meta.json` : metadonnees du document, recopiees par la plateforme sur chacun
de ses morceaux (filtres par type, pays, programme, annee, projet). `corpus` et le prefixe `tsi-` servent au filtre
de fichiers de l'index : d'autres fichiers du workspace ne sont pas indexes. Les types de chaque cle sont
constants d'un fichier a l'autre (la plateforme refuse sinon le televersement) ; une valeur inconnue est omise.
"""

import json
import re
from collections import Counter
from pathlib import Path

from haystack import Document

from tsi_agent.chunking import chunk_file
from tsi_agent.trial_bundle import section as project_section

CORPUS = "dg-reform-tsi"
PREFIX = "tsi-"
FOLDERS = {"projects": "project", "programme": "programme", "deliverables": "deliverable"}
WARN_BYTES = 10_000_000
# Marqueurs de titre en debut de ligne, repetes ("# # x") ou seuls ("#") : sinon faux titre de section.
_HEADING = re.compile(r"^[ \t]{0,3}(?:#+(?:[ \t]+|$))+", re.MULTILINE)
# Type attendu de chaque cle de metadonnees (meme type dans tous les fichiers).
META_TYPES: dict[str, type] = {
    "corpus": str, "doc_type": str, "doc_id": str, "title": str, "countries": list, "programme": str, "year": int,
    "policy_area": str, "results": str, "projects": list, "multi_country": bool, "detail_page": bool,
    "source_url": str, "chunks": int,
}


def deliverable_section(doc: Document) -> str:
    """Morceau de livrable : titre « publication — document, p. N — programme annee — pays », projets lies, URL."""
    meta = doc.meta
    body = _HEADING.sub("", doc.content.strip())
    facts = [f"Deliverable id: {meta.get('doc_id')}"]
    if meta.get("projects"):
        facts.append(f"Projects: {', '.join(meta['projects'])}")
    if meta.get("source_url"):
        facts.append(f"Source: {meta['source_url']}")
    return f"# {meta.get('context_header')}\n\n{body}\n\n{' · '.join(facts)}\n"


def section(doc: Document) -> str:
    return deliverable_section(doc) if doc.meta.get("doc_type") == "deliverable" else project_section(doc)


def file_meta(docs: list[Document]) -> dict:
    """Metadonnees du fichier, depuis celles du premier morceau ; valeurs vides ou inconnues omises."""
    first = docs[0].meta
    doc_type = first.get("doc_type")
    projects = first.get("projects") if doc_type == "deliverable" else [first["doc_id"]] if doc_type == "project" else []
    source_url = first.get("source_url")
    if doc_type == "deliverable":
        source_url = None  # une URL par document du livrable : dans le texte de chaque morceau
    candidates = {
        "corpus": CORPUS, "doc_type": doc_type, "doc_id": first.get("doc_id"), "title": first.get("title"),
        "countries": list(first.get("countries") or []), "programme": first.get("programme"),
        "year": first.get("year"), "policy_area": first.get("policy_area"), "results": first.get("results"),
        "projects": list(projects or []), "multi_country": first.get("multi_country"),
        "detail_page": first.get("detail_page"), "source_url": source_url, "chunks": len(docs),
    }
    meta = {}
    for key, value in candidates.items():
        if value is None or value == "":
            continue
        if META_TYPES[key] is int and isinstance(value, str) and value.isdigit():
            value = int(value)
        meta[key] = value
    return meta


def check_meta_types(metas: list[dict]) -> list[str]:
    """Ecarts de type par rapport a META_TYPES (bool n'est pas accepte pour int)."""
    problems = []
    for meta in metas:
        for key, value in meta.items():
            expected = META_TYPES.get(key)
            if expected is None:
                problems.append(f"{meta.get('doc_id')}: cle inattendue {key}")
            elif not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                problems.append(f"{meta.get('doc_id')}: {key}={value!r} n'est pas {expected.__name__}")
    return problems


def export(output_dir: Path, out_dir: Path) -> dict:
    """Ecrit les fichiers et leurs .meta.json (vide d'abord les tsi-* de out_dir) ; renvoie le manifeste.

    Le manifeste (nombre de fichiers et de morceaux attendus sur la plateforme) est ecrit a cote de out_dir, pas
    dedans : out_dir est televerse tel quel.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{PREFIX}*"):
        old.unlink()
    files: Counter = Counter()
    chunks: Counter = Counter()
    metas: list[dict] = []
    skipped: list[str] = []
    largest = (0, "")
    for folder, doc_type in FOLDERS.items():
        for path in sorted((output_dir / folder).glob("*.md")):
            rel = f"{folder}/{path.name}"
            try:
                docs = chunk_file(rel, path.read_text(encoding="utf-8"))
            except ValueError:
                skipped.append(rel)
                continue
            if not docs:
                skipped.append(rel)
                continue
            name = f"{PREFIX}{doc_type}-{path.name}"
            target = out_dir / name
            target.write_text("\n".join(section(d) for d in docs), encoding="utf-8", newline="\n")
            meta = file_meta(docs)
            Path(f"{target}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            metas.append(meta)
            files[doc_type] += 1
            chunks[doc_type] += len(docs)
            largest = max(largest, (target.stat().st_size, name))
    manifest = {
        "corpus": CORPUS,
        "files": sum(files.values()), "files_by_type": dict(files),
        "chunks": sum(chunks.values()), "chunks_by_type": dict(chunks),
        "largest_file": {"name": largest[1], "mb": round(largest[0] / 1_000_000, 2)},
        "files_over_10mb": largest[0] > WARN_BYTES,
        "meta_type_problems": check_meta_types(metas),
        "skipped": skipped,
    }
    (out_dir.parent / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
