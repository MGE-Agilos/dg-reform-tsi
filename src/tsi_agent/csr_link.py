"""Liaison projet -> sous-parties de CSR par LLM (CLI Claude, sortie JSON), en lot avec cache et reprise.

Candidates d'un projet d'annee y : sous-parties des CSR de ses pays adoptees de y-WINDOW a y-1 (une demande TSI
de l'annee y est deposee a l'automne de y-1, sur les CSR des annees precedentes). Au-dela de 2 pays : les
MAX_PER_COUNTRY plus proches par pays (Jaccard des mots, deterministe) pour borner le prompt ; le LLM juge.
Decisions dans output/csr_links/<stem>.json (rejouees sans LLM tant que le projet et ses candidates ne changent
pas) ; liens ecrits dans le fichier projet : front matter `csrs` et section SECTION_HEADING.
"""

import csv
import hashlib
import json
import os
import random
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.chunking import split_sections
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.countries import name_from_iso
from tsi_agent.csr import Csr
from tsi_agent.fuzzy import prefix_tokens
from tsi_agent.render import _TAIL_HEADINGS, parse_front_matter, render
from tsi_agent.store import _retry_on_lock

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


NAME = "CsrLinker"


def _is_quota(exc: BaseException) -> bool:
    return isinstance(exc, LLMQuotaExceeded) or "RateLimit" in type(exc).__name__


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=1, ensure_ascii=False)

    def write() -> None:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    _retry_on_lock(write)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _apply(path: Path, record: dict[str, Any]) -> bool:
    text = path.read_text(encoding="utf-8")
    new = apply_links(text, record)
    if new == text.replace("\r\n", "\n"):
        return False
    _retry_on_lock(lambda: path.write_text(new, encoding="utf-8"))
    return True


def run_linking(settings: Settings, csrs: list[Csr], generator: Any, *, concurrency: int | None = None,
                limit: int | None = None, project_ids: list[str] | None = None) -> dict[str, Any]:
    """Lie les projets (nouveaux ou modifies) a leurs CSR, puis ecrit les liens de tous les projets deja lies.

    S'arrete proprement sur quota (relancer reprend). Un projet dont la reponse est illisible (2 essais) est
    saute et liste dans `failed`, sans enregistrement : il sera redemande au prochain run.
    """
    root = Path(settings.output_dir)
    links_dir = root / LINKS_DIR
    idx = index_parts(csrs)
    summary: dict[str, Any] = {"projects": 0, "linked": 0, "cached": 0, "no_candidates": 0, "failed": [],
                               "files_updated": 0, "quota_exceeded": False}
    todo: list[tuple[Path, dict, str, list[Candidate]]] = []
    for path in sorted((root / "projects").glob("*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except ValueError:
            continue
        if not isinstance(fm, dict) or not fm.get("project_id"):
            continue
        if project_ids and fm["project_id"] not in project_ids:
            continue
        summary["projects"] += 1
        text, _ = project_input(fm, body)
        cands = select_candidates(fm, text, idx)
        record_path = links_dir / f"{path.stem}.json"
        stored = _read_json(record_path)
        if not cands:
            if not stored or stored.get("status") != "no_candidates":
                _write_json(record_path, {"project_id": fm["project_id"], "status": "no_candidates",
                                          "content_hash": fm.get("content_hash"), "links": [],
                                          "date": date.today().isoformat()})
            summary["no_candidates"] += 1
            continue
        if stored and stored.get("status") == "ok" and stored.get("content_hash") == fm.get("content_hash") \
                and stored.get("candidates_hash") == candidates_hash(cands):
            summary["cached"] += 1
            continue
        todo.append((path, fm, body, cands))
    if limit is not None:
        todo = todo[:limit]
    stop = threading.Event()
    lock = threading.Lock()

    def work(item: tuple[Path, dict, str, list[Candidate]]) -> None:
        path, fm, body, cands = item
        if stop.is_set():
            return
        for attempt in (1, 2):
            try:
                record = link_project(generator, fm, body, cands)
                break
            except ValueError as exc:  # reponse illisible : un second essai
                if attempt == 2:
                    runlog.event(NAME, "error", fm["project_id"], "bad_reply", error=repr(exc)[:300])
                    with lock:
                        summary["failed"].append(fm["project_id"])
                    return
            except Exception as exc:
                if _is_quota(exc):
                    stop.set()
                    runlog.event(NAME, "error", fm["project_id"], "quota_exceeded", error=str(exc)[:300])
                else:
                    runlog.event(NAME, "error", fm["project_id"], "llm_failed", error=repr(exc)[:300])
                    with lock:
                        summary["failed"].append(fm["project_id"])
                return
        _write_json(links_dir / f"{path.stem}.json", record)
        runlog.event(NAME, "link", fm["project_id"], "ok", links=len(record["links"]), basis=record["basis"])
        with lock:
            summary["linked"] += 1

    with ThreadPoolExecutor(max_workers=max(1, concurrency or settings.max_concurrency)) as pool:
        list(pool.map(work, todo))
    summary["quota_exceeded"] = stop.is_set()
    summary["failed"] = sorted(summary["failed"])
    for path in sorted((root / "projects").glob("*.md")):
        record = _read_json(links_dir / f"{path.stem}.json")
        if record and record.get("status") in ("ok", "no_candidates") and _apply(path, record):
            summary["files_updated"] += 1
    return summary


SAMPLE_COLUMNS = ["project_id", "title", "programme", "year", "countries", "basis", "csr_id", "level",
                  "rationale", "csr_text", "csr_assessment", "verdict"]


def write_sample(settings: Settings, csrs: list[Csr], n: int, out: Path, seed: int = 42) -> Path:
    """Echantillon de n projets lies (au moins un lien), stratifie par programme et annee, graine fixe : une ligne
    par lien, colonne `verdict` vide pour la relecture metier."""
    root = Path(settings.output_dir)
    parts = {c.part_id: c for cands in index_parts(csrs).values() for c in cands}
    linked: dict[tuple, list[tuple[dict, dict]]] = defaultdict(list)
    for path in sorted((root / LINKS_DIR).glob("*.json")):
        record = _read_json(path)
        project = root / "projects" / f"{path.stem}.md"
        if not record or not record.get("links") or not project.exists():
            continue
        fm, _ = parse_front_matter(project.read_text(encoding="utf-8").replace("\r\n", "\n"))
        linked[(fm.get("programme"), fm.get("year"))].append((fm, record))
    rng = random.Random(seed)
    strata = sorted(linked, key=lambda k: (str(k[0]), k[1] or 0))
    for key in strata:
        rng.shuffle(linked[key])
    chosen: list[tuple[dict, dict]] = []
    while len(chosen) < n and any(linked[k] for k in strata):
        for key in strata:
            if linked[key] and len(chosen) < n:
                chosen.append(linked[key].pop())
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        for fm, record in chosen:
            for link in record["links"]:
                part = parts.get(link["csr_id"])
                writer.writerow({"project_id": fm.get("project_id"), "title": fm.get("title"),
                                 "programme": fm.get("programme"), "year": fm.get("year"),
                                 "countries": ",".join(fm.get("countries") or []), "basis": record.get("basis"),
                                 "csr_id": link["csr_id"], "level": link["level"], "rationale": link["rationale"],
                                 "csr_text": part.text if part else "", "csr_assessment": part.assessment if part else "",
                                 "verdict": ""})
    return out
