"""Liaison projet -> sous-parties de CSR par LLM (CLI Claude, sortie JSON), en lot avec cache et reprise.

Candidates d'un projet d'annee y : sous-parties des CSR de ses pays adoptees de y-WINDOW a y-1 (une demande TSI
de l'annee y est deposee a l'automne de y-1, sur les CSR des annees precedentes). Au-dela de 2 pays : les
MAX_PER_COUNTRY plus proches par pays (Jaccard des mots, deterministe) pour borner le prompt ; le LLM juge.
Decisions dans output/csr_links/<stem>.json (rejouees sans LLM tant que le projet et ses candidates ne changent
pas) ; liens ecrits dans le fichier projet : front matter `csrs` et section SECTION_HEADING.
"""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass

from tsi_agent.chunking import split_sections
from tsi_agent.csr import Csr
from tsi_agent.fuzzy import prefix_tokens

WINDOW = 3
MAX_PER_COUNTRY = 15
MANY_COUNTRIES = 2
MAX_LINKS = 5
PROJECT_CHARS = 6000
LINKS_DIR = "csr_links"
SECTION_HEADING = "## Supported CSRs (AI-assessed)"
SECTION_NAME = SECTION_HEADING.removeprefix("## ")
# Sections du fichier projet exclues de l'entree du LLM : listing, livrables et liens CSR deja ecrits.
_SKIP = {"Sources", "Listed in country factsheets as", "Deliverables (AI-generated summary)", SECTION_NAME}


@dataclass(frozen=True)
class Candidate:
    part_id: str
    csr_id: str
    country: str
    year: int
    text: str
    policy_areas: list[str]
    assessment: str | None


def index_parts(csrs: list[Csr]) -> dict[tuple[str, int], list[Candidate]]:
    """Sous-parties candidates par (pays, annee) ; une CSR sans sous-partie est sa propre candidate."""
    idx: dict[tuple[str, int], list[Candidate]] = defaultdict(list)
    for csr in csrs:
        parts = csr.subparts or ([csr.full] if csr.full else [])
        for p in parts:
            idx[(csr.country, csr.year)].append(Candidate(p.part_id, csr.csr_id, csr.country, csr.year, p.text,
                                                          list(p.policy_areas), p.assessment))
    return dict(idx)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def select_candidates(fm: dict, text: str, idx: dict[tuple[str, int], list[Candidate]]) -> list[Candidate]:
    """Candidates d'un projet (front matter + texte d'entree), par pays puis annee puis numero."""
    year, countries = fm.get("year"), list(dict.fromkeys(fm.get("countries") or []))
    if not isinstance(year, int) or not countries:
        return []
    tokens = prefix_tokens(f"{fm.get('title') or ''} {text[:2000]}")
    out: list[Candidate] = []
    for country in countries:
        found = [c for y in range(year - WINDOW, year) for c in idx.get((country, y), [])]
        if len(countries) > MANY_COUNTRIES and len(found) > MAX_PER_COUNTRY:
            ranked = sorted(found, key=lambda c: -_jaccard(tokens, prefix_tokens(c.text)))
            keep = {c.part_id for c in ranked[:MAX_PER_COUNTRY]}
            found = [c for c in found if c.part_id in keep]
        out.extend(found)
    return out


def candidates_hash(cands: list[Candidate]) -> str:
    return "sha1:" + hashlib.sha1("|".join(sorted(c.part_id for c in cands)).encode("utf-8")).hexdigest()


def project_input(fm: dict, body: str) -> tuple[str, str]:
    """(texte transmis au LLM, base) : sections officielles et resume si page detail, sinon titres seuls."""
    if not fm.get("detail_page"):
        listed = [t for t in fm.get("listing_titles") or [] if t]
        lines = [f"Title: {fm.get('title')}"] + ([f"Listed as: {'; '.join(listed)}"] if listed else [])
        return "\n".join(lines), "title_only"
    parts = [f"{name}: {text}" for name, text in split_sections(body) if name not in _SKIP]
    return re.sub(r"[ \t]+", " ", "\n\n".join(parts))[:PROJECT_CHARS], "full_text"
