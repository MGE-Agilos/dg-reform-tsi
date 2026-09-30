"""Decoupage des fichiers extraits en morceaux pour la couche vectorisee (deterministe, sans LLM)."""

import re
from typing import Any

from haystack import Document

from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.render import parse_front_matter

# A incrementer a chaque changement des regles ci-dessous : force une revectorisation complete.
CHUNKER_VERSION = 1
SPLIT_THRESHOLD_WORDS = 300
WINDOW_WORDS = 250
OVERLAP_WORDS = 40
SENTENCE_LOOKBACK_WORDS = 50

INTRO = "Introduction"
SUMMARY = "Summary (AI-generated)"
# Listes d'URL et titres de listing : deja en metadonnees, sans valeur pour la recherche.
SKIPPED_SECTIONS = {"Sources", "Listed in country factsheets as"}
PROJECT_FIELDS = ("title", "programme", "year", "countries", "multi_country", "policy_area", "detail_page",
                  "results", "spotlight", "highlighted", "listed")

_TITLE = re.compile(r"\A# [^\n]*(?:\n|\Z)")
_HEADING = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


def split_sections(body: str) -> list[tuple[str, str]]:
    """(nom de section, texte) dans l'ordre ; le texte avant la premiere section `##` est l'introduction."""
    body = _TITLE.sub("", body.lstrip("\n"), count=1)
    parts = _HEADING.split(body)
    sections = [(INTRO, parts[0].strip())]
    sections += [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    return [(name, text) for name, text in sections if text and name not in SKIPPED_SECTIONS]


def split_words(text: str, threshold: int = SPLIT_THRESHOLD_WORDS, window: int = WINDOW_WORDS,
                overlap: int = OVERLAP_WORDS) -> list[str]:
    """Texte court : tel quel. Texte long : fenetres de `window` mots, coupees en fin de phrase si possible."""
    words = text.split()
    if len(words) <= threshold:
        return [text]
    chunks: list[str] = []
    start = 0
    while True:
        end = min(start + window, len(words))
        if end < len(words):
            for cut in range(end, start + window - SENTENCE_LOOKBACK_WORDS, -1):
                if _SENTENCE_END.search(words[cut - 1]):
                    end = cut
                    break
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            return chunks
        start = end - overlap


def context_header(fm: dict[str, Any]) -> str:
    """« Titre — Programme Annee — Pays » : ajoute au texte vectorise pour qu'un morceau garde son contexte."""
    parts = [str(fm.get("title") or "").strip()]
    parts.append(" ".join(str(v) for v in (fm.get("programme"), fm.get("year")) if v))
    countries = [name_from_iso(c) if c in COUNTRIES else str(c) for c in fm.get("countries") or []]
    parts.append(", ".join(countries))
    return " — ".join(p for p in parts if p)


def chunk_file(rel_path: str, text: str) -> list[Document]:
    """Morceaux d'un fichier projet ou programme. ValueError si le front matter est absent ou invalide."""
    fm, body = parse_front_matter(text.replace("\r\n", "\n"))
    if not isinstance(fm, dict):
        raise ValueError("front matter is not a mapping")
    doc_id = fm.get("project_id") or fm.get("doc_id")
    if not doc_id:
        raise ValueError("front matter has no project_id or doc_id")
    urls = fm.get("source_urls") or []
    base: dict[str, Any] = {
        "doc_id": doc_id,
        "doc_type": "project" if fm.get("project_id") else "programme",
        "source_file": rel_path,
        "source_url": fm.get("source_url") or (urls[0] if urls else None),
        "context_header": context_header(fm),
        **{field: fm.get(field) for field in PROJECT_FIELDS},
    }
    # Identifiant par chemin de fichier, pas par doc_id : deux fichiers peuvent partager le meme project_id/doc_id
    # (front matter duplique ou erreur d'extraction) sans faire collision ni s'ecraser l'un l'autre.
    stem = rel_path.removesuffix(".md")
    docs: list[Document] = []
    for section, section_text in split_sections(body):
        for piece in split_words(section_text):
            index = len(docs)
            docs.append(Document(id=f"{stem}#{index}", content=piece,
                                 meta={**base, "section": section, "chunk_index": index,
                                       "ai_generated": section == SUMMARY}))
    return docs
