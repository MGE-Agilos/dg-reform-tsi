"""Liaison projet -> sous-parties de CSR par LLM (CLI Claude, sortie JSON), en lot avec cache et reprise.

Candidates d'un projet d'annee y : sous-parties des CSR de ses pays adoptees de y-WINDOW a y-1 (une demande TSI
de l'annee y est deposee a l'automne de y-1, sur les CSR des annees precedentes). Au-dela de 2 pays : les
MAX_PER_COUNTRY plus proches par pays (Jaccard des mots, deterministe) pour borner le prompt ; le LLM juge.
Decisions dans output/csr_links/<stem>.json (rejouees sans LLM tant que le projet et ses candidates ne changent
pas) ; liens ecrits dans le fichier projet : front matter `csrs` et section SECTION_HEADING.
"""

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from haystack.dataclasses import ChatMessage

from tsi_agent.chunking import split_sections
from tsi_agent.countries import name_from_iso
from tsi_agent.csr import Csr
from tsi_agent.fuzzy import prefix_tokens
from tsi_agent.render import _TAIL_HEADINGS, parse_front_matter, render

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


LINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"links": {"type": "array", "items": {
        "type": "object",
        "properties": {"csr_id": {"type": "string"}, "level": {"type": "string", "enum": ["direct", "partial"]},
                       "rationale": {"type": "string"}},
        "required": ["csr_id", "level", "rationale"]}}},
    "required": ["links"],
}

SYSTEM_PROMPT = ("You link European Commission technical support projects (DG REFORM: TSI, SRSP) to the "
                 "country-specific recommendations (CSRs) of the European Semester. You answer only from the "
                 "texts given.")

INSTRUCTIONS = f"""Below are a technical support project and CANDIDATE CSR sub-parts addressed to its \
country or countries in the years before the request. Say which CSR sub-parts the project supports.

- "direct": the project's objective is to design or implement what the CSR sub-part recommends.
- "partial": the project contributes to one aspect of the CSR sub-part, or to a precondition for it.
- Do not link a CSR sub-part that only shares a broad theme (e.g. "public finances") without a concrete \
connection to what the project does. Most projects support 0 to 3 sub-parts; return at most {MAX_LINKS}.

For each link give csr_id (a candidate id exactly as written), level, and a one-sentence rationale that names \
what the project does. Return an empty list when no candidate is supported."""


def build_prompt(fm: dict, text: str, basis: str, cands: list[Candidate]) -> str:
    countries = ", ".join(name_from_iso(c) for c in fm.get("countries") or [])
    head = [f"Title: {fm.get('title')}",
            f"Programme and year: {' '.join(str(v) for v in (fm.get('programme'), fm.get('year')) if v)}",
            f"Countries: {countries}"]
    if basis == "title_only":
        head.append("Note: only the title of this project is published; link only when the title makes the "
                    "connection clear.")
    lines = [f"- {c.part_id} ({name_from_iso(c.country)} {c.year}; {'; '.join(c.policy_areas) or 'no area'}; "
             f"latest assessment: {c.assessment or 'none'}): {c.text}" for c in cands]
    return "\n\n".join([INSTRUCTIONS, "## Project", "\n".join(head), text, "## Candidate CSR sub-parts",
                        "\n".join(lines)])


def parse_links(text: str, cands: list[Candidate]) -> list[dict[str, str]]:
    """Liens valides : id parmi les candidates, niveau connu, sans doublon, au plus MAX_LINKS, ordre du LLM."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    raw = json.loads(text[start:end + 1]).get("links")
    if not isinstance(raw, list):
        raise ValueError("no links list")
    known = {c.part_id for c in cands}
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid, level = item.get("csr_id"), item.get("level")
        if cid in known and level in ("direct", "partial") and cid not in {o["csr_id"] for o in out}:
            out.append({"csr_id": cid, "level": level, "rationale": " ".join(str(item.get("rationale") or "").split())})
    return out[:MAX_LINKS]


def link_project(generator: Any, fm: dict, body: str, cands: list[Candidate]) -> dict[str, Any]:
    """Enregistrement de liaison d'un projet (un appel LLM). Exceptions du generateur propagees (quota...)."""
    text, basis = project_input(fm, body)
    reply = generator.run(messages=[ChatMessage.from_system(SYSTEM_PROMPT),
                                    ChatMessage.from_user(build_prompt(fm, text, basis, cands))])["replies"][0]
    return {"project_id": fm.get("project_id"), "status": "ok", "basis": basis,
            "content_hash": fm.get("content_hash"), "candidates_hash": candidates_hash(cands),
            "candidates": [c.part_id for c in cands], "links": parse_links(reply.text or "", cands),
            "model": (reply.meta or {}).get("model"), "date": date.today().isoformat()}


def links_section(record: dict[str, Any]) -> str | None:
    links = record.get("links") or []
    if not links:
        return None
    lines = [SECTION_HEADING, "",
             "Country-specific recommendations (European Semester) that this project supports, as assessed by an "
             "AI model from the project text" + (" (based on the project title only)" if record.get("basis") ==
                                                 "title_only" else "") + ":", ""]
    lines += [f"- {link['csr_id']} ({link['level']}): {link['rationale']}" for link in links]
    return "\n".join(lines)


def _replace_section(body: str, section: str | None) -> str:
    """Retire la section des liens existante et insere `section` avant la partie listing (ou a la fin)."""
    lines = body.split("\n")
    if SECTION_HEADING in lines:
        start = lines.index(SECTION_HEADING)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        lines = lines[:start] + lines[end:]
    cut = next((i for i, line in enumerate(lines) if line in _TAIL_HEADINGS), len(lines))
    head, tail = "\n".join(lines[:cut]).strip("\n"), "\n".join(lines[cut:]).strip("\n")
    return "\n\n".join(p for p in (head, section, tail) if p)


def apply_links(text: str, record: dict[str, Any]) -> str:
    """Texte du fichier projet avec `csrs` (front matter) et la section des liens a jour ; idempotent."""
    fm, body = parse_front_matter(text.replace("\r\n", "\n"))
    ids = [link["csr_id"] for link in record.get("links") or []]
    fm = {k: v for k, v in fm.items() if k != "csrs"}
    if ids:
        fm["csrs"] = ids
    return render(fm, _replace_section(body, links_section(record)))
