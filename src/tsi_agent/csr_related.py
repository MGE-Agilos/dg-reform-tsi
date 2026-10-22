"""Projets passes sur le meme sujet qu'une recommandation de la Commission (`csr-related`), en lot avec cache et
reprise. Toujours par le CLI Claude (souscription), quel que soit `llm_provider`.

Pour chaque recommandation (output/csr/csr-<ISO>-<annee>-<n>.md, annee 2026 par defaut) :
1. candidats : couche vectorisee locale (textes projet ; une requete pour le texte entier et une par phrase, meilleur
   score par projet), a defaut recouvrement de mots avec le titre et le resume des projets ; MAX_SAME projets du
   meme pays et MAX_OTHER projets d'autres pays, d'une annee au plus egale a celle de la recommandation ;
2. un appel LLM a sortie JSON qui juge la pertinence (high / medium ; low et hors sujet ecartes).

Decisions dans output/csr_related/<csr_id>.json, rejouees sans LLM tant que `input_hash` (version du prompt, texte de
la recommandation) ne change pas : une relance saute les recommandations deja traitees (`redo` pour les refaire, par
exemple apres un nouvel index). Sur quota, le lot s'arrete proprement ; la relance reprend.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.chat.service import find_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.csr_link import _read_json, _write_json
from tsi_agent.fuzzy import prefix_tokens
from tsi_agent.render import parse_front_matter

PROMPT_VERSION = 1
RELATED_DIR = "csr_related"
MAX_SAME = 12
MAX_OTHER = 8
MAX_SENTENCES = 8
SAME_TOP_K = 150  # morceaux lus par requete, filtre sur le pays
OTHER_TOP_K = 300  # morceaux lus par requete, sans filtre de pays
CANDIDATE_SUMMARY_CHARS = 500
RELEVANCE = ("high", "medium")
SCOPES = ("same_country", "other_country")
NAME = "CsrRelated"

RELATED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"related": {"type": "array", "items": {
        "type": "object",
        "properties": {"project_id": {"type": "string"},
                       "relevance": {"type": "string", "enum": ["high", "medium", "low"]},
                       "scope": {"type": "string", "enum": list(SCOPES)},
                       "rationale": {"type": "string"}},
        "required": ["project_id", "relevance", "scope", "rationale"]}}},
    "required": ["related"],
}

SYSTEM_PROMPT = ("You identify past European Commission technical support projects (DG REFORM: TSI, SRSP) that "
                 "worked on the same subject as a country-specific recommendation (CSR) of the European Semester. "
                 "You answer only from the texts given.")

INSTRUCTIONS = """Below are a CSR proposed by the Commission and CANDIDATE past technical support projects, from \
the same Member State and from other Member States. Say which projects worked on the same subject as (a part of) \
the CSR: the same reform, policy instrument, institution or problem.

- "high": the project's objective directly addresses a measure the CSR recommends.
- "medium": the project addresses a closely related subject; its experience is clearly useful for the CSR.
- Leave out projects that only share a broad theme (e.g. "public finances", "digitalisation") without a concrete \
connection, and the projects you would rate "low".
- Judge from the project's title and summary, not from its policy-area tag alone.

For each project kept give project_id (exactly as written), relevance, scope ("same_country" for a project of the \
CSR's Member State, "other_country" otherwise) and a rationale of at most two sentences that names the part of the \
CSR and what the project did. Return an empty list when no candidate is relevant."""


@dataclass(frozen=True)
class Recommendation:
    csr_id: str
    country: str
    year: int
    number: str
    title: str
    text: str


def _number_key(number: str) -> tuple[int, ...]:
    return tuple(int(n) if n.isdigit() else 0 for n in str(number).split("."))


def recommendation_text(body: str) -> str:
    """Texte de la recommandation : premier paragraphe apres le titre `# ...` de la fiche."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    for p in paragraphs:
        if not p.startswith("#"):
            return " ".join(p.split())
    return ""


def load_recommendations(output_dir: str | Path, year: int,
                         countries: list[str] | None = None) -> list[Recommendation]:
    """Recommandations (niveau recommandation, pas les sous-parties) d'une annee, par pays puis numero."""
    out = []
    for path in sorted((Path(output_dir) / "csr").glob(f"csr-*-{year}-*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(fm, dict) or not fm.get("csr_id") or fm.get("year") != year:
            continue
        if fm.get("csr_level") not in (None, "recommendation") or "." in str(fm.get("number") or ""):
            continue
        country = (fm.get("countries") or [str(fm["csr_id"]).split("-")[1]])[0]
        if countries and country not in countries:
            continue
        text = recommendation_text(body)
        if text:
            out.append(Recommendation(str(fm["csr_id"]), country, year, str(fm.get("number") or ""),
                                      str(fm.get("title") or fm["csr_id"]), text))
    return sorted(out, key=lambda r: (r.country, _number_key(r.number)))


def input_hash(rec: Recommendation) -> str:
    return "sha1:" + hashlib.sha1(f"{PROMPT_VERSION}|{rec.csr_id}|{rec.text}".encode("utf-8")).hexdigest()


def sentences(text: str, limit: int = MAX_SENTENCES) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z])", text) if len(s.strip()) > 30]
    return parts[:limit]


class CandidateFinder(Protocol):
    source: str

    def find(self, rec: Recommendation) -> tuple[list[str], list[str]]:
        """(ids des projets du meme pays, ids des projets d'autres pays), du plus proche au moins proche."""
        ...


def _eligible(project: dict[str, Any], rec: Recommendation) -> bool:
    year = project.get("year")
    return year is None or year <= rec.year


class KeywordCandidates:
    """Repli sans index : recouvrement des mots (prefixes) de la recommandation et du titre + resume du projet."""
    source = "keyword"

    def __init__(self, projects: list[dict[str, Any]], max_same: int = MAX_SAME, max_other: int = MAX_OTHER):
        self.projects = projects
        self.max_same, self.max_other = max_same, max_other
        self._tokens = {p["project_id"]: prefix_tokens(f"{p.get('title') or ''} {p.get('summary') or ''}")
                        for p in projects}

    def find(self, rec: Recommendation) -> tuple[list[str], list[str]]:
        query = prefix_tokens(rec.text)
        scored = []
        for p in self.projects:
            tokens = self._tokens[p["project_id"]]
            if not tokens or not query or not _eligible(p, rec):
                continue
            score = len(query & tokens) / math.sqrt(len(query) * len(tokens))
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda sp: (-sp[0], sp[1]["project_id"]))
        same = [p["project_id"] for _, p in scored if rec.country in (p.get("countries") or [])]
        other = [p["project_id"] for _, p in scored if rec.country not in (p.get("countries") or [])]
        return same[:self.max_same], other[:self.max_other]


class VectorCandidates:
    """Couche vectorisee locale : morceaux de type projet, meilleur score par projet sur le texte entier et chaque
    phrase de la recommandation."""
    source = "vector"

    def __init__(self, text_embedder: Any, retriever: Any, projects: list[dict[str, Any]],
                 max_same: int = MAX_SAME, max_other: int = MAX_OTHER):
        self.text_embedder = text_embedder
        self.retriever = retriever
        self.by_id = {p["project_id"]: p for p in projects}
        self.max_same, self.max_other = max_same, max_other

    def _best(self, rec: Recommendation, countries: list[str] | None, top_k: int) -> dict[str, float]:
        from tsi_agent.chat.tools import build_filters
        best: dict[str, float] = {}
        for query in [rec.text, *sentences(rec.text)]:
            embedding = self.text_embedder.run(text=query)["embedding"]
            docs = self.retriever.run(query_embedding=embedding, top_k=top_k,
                                      filters=build_filters(doc_type="project", countries=countries))["documents"]
            for doc in docs:
                pid = doc.meta.get("doc_id")
                project = self.by_id.get(pid)
                if project is None or doc.score is None or not _eligible(project, rec):
                    continue
                if pid not in best or doc.score > best[pid]:
                    best[pid] = doc.score
        return best

    @staticmethod
    def _ranked(best: dict[str, float]) -> list[str]:
        return sorted(best, key=lambda pid: (-best[pid], pid))

    def find(self, rec: Recommendation) -> tuple[list[str], list[str]]:
        same = [pid for pid in self._ranked(self._best(rec, [rec.country], SAME_TOP_K))
                if rec.country in (self.by_id[pid].get("countries") or [])]
        other = [pid for pid in self._ranked(self._best(rec, None, OTHER_TOP_K))
                 if rec.country not in (self.by_id[pid].get("countries") or [])]
        return same[:self.max_same], other[:self.max_other]


def _candidate_line(p: dict[str, Any]) -> str:
    programme_year = " ".join(str(v) for v in (p.get("programme"), p.get("year")) if v)
    countries = ", ".join(name_from_iso(c) for c in p.get("countries") or [] if c in COUNTRIES)
    head = "; ".join(v for v in (programme_year, countries, p.get("policy_area") or "") if v)
    summary = (p.get("summary") or "")[:CANDIDATE_SUMMARY_CHARS]
    return f"- {p['project_id']} — {p.get('title') or p['project_id']} ({head})" + (f": {summary}" if summary else "")


def build_prompt(rec: Recommendation, same: list[dict[str, Any]], other: list[dict[str, Any]]) -> str:
    country = name_from_iso(rec.country) if rec.country in COUNTRIES else rec.country
    parts = [INSTRUCTIONS, "## CSR", f"{rec.title} ({country}, {rec.year})", rec.text,
             f"## Candidate projects in {country} (scope same_country)",
             "\n".join(_candidate_line(p) for p in same) or "(none)",
             "## Candidate projects in other Member States (scope other_country)",
             "\n".join(_candidate_line(p) for p in other) or "(none)"]
    return "\n\n".join(parts)


def _two_sentences(text: str) -> str:
    clean = " ".join(str(text or "").split())
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return " ".join(parts[:2])


def parse_related(text: str, same: list[str], other: list[str]) -> list[dict[str, str]]:
    """Projets retenus : id parmi les candidats, pertinence high/medium (low ecarte), sans doublon, ordre du LLM.
    La portee vient du groupe du candidat (pas de la reponse). ValueError si la reponse n'est pas lisible."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    raw = json.loads(text[start:end + 1]).get("related")
    if not isinstance(raw, list):
        raise ValueError("no related list")
    scope_of = {**{pid: "other_country" for pid in other}, **{pid: "same_country" for pid in same}}
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid, relevance = item.get("project_id"), str(item.get("relevance") or "").lower()
        if pid in scope_of and relevance in RELEVANCE and pid not in {o["project_id"] for o in out}:
            out.append({"project_id": pid, "relevance": relevance, "scope": scope_of[pid],
                        "rationale": _two_sentences(item.get("rationale"))})
    return out


def relate(generator: Any, rec: Recommendation, same: list[dict[str, Any]],
           other: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str | None]:
    """Un appel LLM : (projets retenus, modele). Exceptions du generateur propagees (quota...)."""
    reply = generator.run(messages=[ChatMessage.from_system(SYSTEM_PROMPT),
                                    ChatMessage.from_user(build_prompt(rec, same, other))])["replies"][0]
    related = parse_related(reply.text or "", [p["project_id"] for p in same], [p["project_id"] for p in other])
    return related, (reply.meta or {}).get("model")


def is_done(record: dict[str, Any] | None, rec: Recommendation) -> bool:
    return bool(record) and record.get("status") in ("ok", "no_candidates") and \
        record.get("input_hash") == input_hash(rec)


def run_related(output_dir: str | Path, recommendations: list[Recommendation], projects: list[dict[str, Any]],
                generator: Any, finder_factory: Callable[[], CandidateFinder], *, limit: int | None = None,
                redo: bool = False, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Traite les recommandations non encore jugees (ou toutes avec `redo`) ; `limit` borne les appels LLM.

    `finder_factory` n'est appelee que s'il reste du travail (l'index et l'embedder ne sont pas charges pour rien).
    Une reponse illisible (2 essais) ou une erreur du CLI : recommandation notee en echec, sans enregistrement
    (redemandee a la prochaine relance). Quota : arret du lot."""
    root = Path(output_dir) / RELATED_DIR
    by_id = {p["project_id"]: p for p in projects}
    summary: dict[str, Any] = {"recommendations": len(recommendations), "done": [], "cached": 0,
                               "no_candidates": [], "failed": [], "remaining": [], "quota_exceeded": False,
                               "related": 0, "candidates_source": None}
    todo = []
    for rec in recommendations:
        if not redo and is_done(_read_json(root / f"{rec.csr_id}.json"), rec):
            summary["cached"] += 1
        else:
            todo.append(rec)
    if limit is not None:
        summary["remaining"] = [r.csr_id for r in todo[max(limit, 0):]]
        todo = todo[:max(limit, 0)]
    if not todo:
        return summary
    finder = finder_factory()
    summary["candidates_source"] = finder.source
    for i, rec in enumerate(todo, start=1):
        same_ids, other_ids = finder.find(rec)
        same = [by_id[pid] for pid in same_ids if pid in by_id]
        other = [by_id[pid] for pid in other_ids if pid in by_id]
        record: dict[str, Any] = {"csr_id": rec.csr_id, "country": rec.country, "year": rec.year,
                                  "input_hash": input_hash(rec), "prompt_version": PROMPT_VERSION,
                                  "candidates_source": finder.source,
                                  "candidates": {"same_country": [p["project_id"] for p in same],
                                                 "other_country": [p["project_id"] for p in other]},
                                  "date": date.today().isoformat()}
        if not same and not other:
            _write_json(root / f"{rec.csr_id}.json", {**record, "status": "no_candidates", "related": []})
            summary["no_candidates"].append(rec.csr_id)
            log(f"[{i}/{len(todo)}] {rec.csr_id} : aucun candidat")
            continue
        related = model = error = None
        for attempt in (1, 2):
            try:
                related, model = relate(generator, rec, same, other)
                break
            except ValueError as exc:  # reponse illisible : un second essai
                error = f"bad reply: {exc}"
            except Exception as exc:
                if find_cause(exc, LLMQuotaExceeded):
                    summary["quota_exceeded"] = True
                    runlog.event(NAME, "error", rec.csr_id, "quota_exceeded", error=str(exc)[:300])
                    log(f"[{i}/{len(todo)}] {rec.csr_id} : quota de la souscription Claude atteint, arret "
                        "(relancer plus tard reprend)")
                    summary["remaining"] = [r.csr_id for r in todo[i - 1:]] + summary["remaining"]
                    return summary
                error = f"{type(exc).__name__}: {exc}"
                break
        if related is None:
            runlog.event(NAME, "error", rec.csr_id, "llm_failed", error=str(error)[:300])
            summary["failed"].append({"csr_id": rec.csr_id, "error": str(error)[:300]})
            log(f"[{i}/{len(todo)}] {rec.csr_id} : ECHEC {str(error)[:200]}")
            continue
        _write_json(root / f"{rec.csr_id}.json", {**record, "status": "ok", "related": related, "model": model})
        runlog.event(NAME, "related", rec.csr_id, "ok", related=len(related), candidates=len(same) + len(other))
        summary["done"].append(rec.csr_id)
        summary["related"] += len(related)
        high = sum(1 for r in related if r["relevance"] == "high")
        log(f"[{i}/{len(todo)}] {rec.csr_id} : {len(related)} projet(s) retenu(s) dont {high} high, sur "
            f"{len(same)} + {len(other)} candidats")
    return summary


def load_related_rows(output_dir: str | Path) -> list[dict[str, Any]]:
    """Lignes `csr_related_projects` depuis output/csr_related/*.json (enregistrements `ok`), sans doublon."""
    rows, seen = [], set()
    for path in sorted((Path(output_dir) / RELATED_DIR).glob("*.json")):
        record = _read_json(path)
        if not isinstance(record, dict) or record.get("status") != "ok" or not record.get("csr_id"):
            continue
        for item in record.get("related") or []:
            if not isinstance(item, dict):
                continue
            key = (record["csr_id"], item.get("project_id"))
            if item.get("project_id") and item.get("relevance") in RELEVANCE and item.get("scope") in SCOPES \
                    and key not in seen:
                seen.add(key)
                rows.append({"csr_id": record["csr_id"], "project_id": item["project_id"],
                             "relevance": item["relevance"], "scope": item["scope"],
                             "rationale": item.get("rationale") or ""})
    return rows
