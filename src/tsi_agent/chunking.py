"""Decoupage des fichiers extraits en morceaux pour la couche vectorisee (deterministe, sans LLM)."""

import re
from typing import Any

from haystack import Document

from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.render import parse_front_matter

# A incrementer a chaque changement des regles ci-dessous : force une revectorisation complete.
# Le type csr a ete ajoute sans incrementer CHUNKER_VERSION (morceaux existants inchanges).
# csrs et la section des liens CSR ajoutes sans changer CHUNKER_VERSION : presents dans les fichiers revectorises
# (csr-link reecrit les fichiers lies).
CHUNKER_VERSION = 3  # 2 : section Deliverables (resume IA) ; 3 : texte integral des livrables, un morceau par page
SPLIT_THRESHOLD_WORDS = 300
WINDOW_WORDS = 250
OVERLAP_WORDS = 40
SENTENCE_LOOKBACK_WORDS = 50

INTRO = "Introduction"
SUMMARY = "Summary (AI-generated)"
DELIVERABLES = "Deliverables (AI-generated summary)"
CSR_LINKS = "Supported CSRs (AI-assessed)"
AI_SECTIONS = {SUMMARY, DELIVERABLES, CSR_LINKS}
# Listes d'URL et titres de listing : deja en metadonnees, sans valeur pour la recherche.
SKIPPED_SECTIONS = {"Sources", "Listed in country factsheets as"}
# listing_titles : titres des listings (recherche par mots du titre de find_projects). Ajoute sans changer
# CHUNKER_VERSION : present dans les fichiers revectorises ; `index --full` pour toute la couche.
PROJECT_FIELDS = ("title", "programme", "year", "countries", "multi_country", "policy_area", "detail_page",
                  "results", "spotlight", "highlighted", "listed", "listing_titles", "csrs")

_TITLE = re.compile(r"\A# [^\n]*(?:\n|\Z)")
_HEADING = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)
_DOCUMENT = re.compile(r"^## (.+?)[ \t]*\n\nSource: (.*?)[ \t]*$", re.MULTILINE)
_PAGE = re.compile(r"^--- page (\d+) ---[ \t]*$", re.MULTILINE)
MIN_PAGE_WORDS = 5
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


def split_pages(text: str) -> list[tuple[int | None, str]]:
    """(numero de page, texte) d'apres les marqueurs `--- page N ---` ; sans marqueur (Word) : un bloc, page None."""
    parts = _PAGE.split(text)
    if len(parts) == 1:
        return [(None, text.strip())]
    return [(int(parts[i]), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def chunk_deliverable(rel_path: str, fm: dict[str, Any], body: str, **split: int) -> list[Document]:
    """Texte integral d'une publication : un ou plusieurs morceaux par page de chaque document.

    `split` (threshold, window, overlap) : passe a split_words (banc de recherche) ; defauts inchanges sinon.
    """
    countries = [name_from_iso(c) if c in COUNTRIES else str(c) for c in fm.get("countries") or []]
    programme_year = " ".join(str(v) for v in (fm.get("programme"), fm.get("year")) if v)
    title = str(fm.get("title") or "").strip()
    base: dict[str, Any] = {
        "doc_id": fm["publication_id"], "doc_type": "deliverable", "source_file": rel_path, "title": fm.get("title"),
        "date": fm.get("date"), "projects": list(fm.get("projects") or []), "ai_generated": False,
        "countries": fm.get("countries"), "year": fm.get("year"), "programme": fm.get("programme"),
        "policy_area": fm.get("policy_area"), "detail_page": None,
    }
    stem = rel_path.removesuffix(".md")
    parts = _DOCUMENT.split(_TITLE.sub("", body.lstrip("\n"), count=1))
    docs: list[Document] = []
    for i in range(1, len(parts), 3):
        document, url, text = parts[i].strip(), parts[i + 1], parts[i + 2]
        for page, page_text in split_pages(text):
            if len(page_text.split()) < MIN_PAGE_WORDS:
                continue
            where = f"{document}, p. {page}" if page is not None else document
            header = " — ".join(p for p in (title, where, programme_year, ", ".join(countries)) if p)
            for piece in split_words(page_text, **split):
                index = len(docs)
                docs.append(Document(id=f"{stem}#{index}", content=piece,
                                     meta={**base, "section": document, "page": page, "source_url": url or None,
                                           "chunk_index": index, "context_header": header}))
    return docs


def chunk_csr(rel_path: str, fm: dict[str, Any], body: str, **split: int) -> list[Document]:
    """Fiche CSR : texte complet (introduction), evaluation, sous-parties. Pas de domaine de projet (taxonomies
    differentes) ni de programme."""
    base: dict[str, Any] = {
        "doc_id": fm["csr_id"], "doc_type": "csr", "source_file": rel_path, "source_url": fm.get("source_url"),
        "context_header": context_header(fm), "title": fm.get("title"), "year": fm.get("year"),
        "countries": fm.get("countries"), "programme": None, "policy_area": None, "detail_page": None,
    }
    stem = rel_path.removesuffix(".md")
    docs: list[Document] = []
    for section, section_text in split_sections(body):
        for piece in split_words(section_text, **split):
            index = len(docs)
            docs.append(Document(id=f"{stem}#{index}", content=piece,
                                 meta={**base, "section": section, "chunk_index": index, "ai_generated": False}))
    return docs


def chunk_file(rel_path: str, text: str, *, threshold: int = SPLIT_THRESHOLD_WORDS, window: int = WINDOW_WORDS,
               overlap: int = OVERLAP_WORDS) -> list[Document]:
    """Morceaux d'un fichier projet, programme, livrable ou CSR. ValueError si le front matter est absent ou invalide.

    threshold/window/overlap : taille des morceaux ; seul le banc de recherche les change (CHUNKER_VERSION
    ne concerne que les valeurs par defaut, celles de la couche vectorisee).
    """
    split = {"threshold": threshold, "window": window, "overlap": overlap}
    fm, body = parse_front_matter(text.replace("\r\n", "\n"))
    if not isinstance(fm, dict):
        raise ValueError("front matter is not a mapping")
    if fm.get("publication_id"):
        return chunk_deliverable(rel_path, fm, body, **split)
    if fm.get("csr_id"):
        return chunk_csr(rel_path, fm, body, **split)
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
        for piece in split_words(section_text, **split):
            index = len(docs)
            docs.append(Document(id=f"{stem}#{index}", content=piece,
                                 meta={**base, "section": section, "chunk_index": index,
                                       "ai_generated": section in AI_SECTIONS}))
    return docs
