"""Arbitrage LLM : une page du sitemap restee sans fusion est-elle un projet thin deja connu, sous un autre titre ?

Les regles deterministes (cle titre, regle floue difflib) ratent les reformulations ("employment gender gap"
contre "gender employment gap"). Pour chaque page du sitemap sans fusion, on choisit au plus MAX_CANDIDATES
projets thin compatibles (programme, annee a un an pres, pays) et assez proches (Jaccard des mots >=
JACCARD_FLOOR), puis un LLM a sortie structuree tranche par lots. Seules les reponses de confiance high/medium
fusionnent. Les decisions sont gardees dans output/state/merge_decisions.json (relisibles, modifiables :
le champ "override" = id thin ou "none" l'emporte) : un nouveau run ne rappelle pas le LLM.
"""

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from haystack import component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ChatMessage
from haystack.utils import deserialize_chatgenerator_inplace

from tsi_agent import runlog
from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.components.discoverer import combine
from tsi_agent.fuzzy import countries_in_text, prefix_tokens
from tsi_agent.http import DEFAULT_USER_AGENT, shared_fetcher
from tsi_agent.models import ProjectRef
from tsi_agent.parsers.detail import parse_detail
from tsi_agent.store import _retry_on_lock

NAME = "SitemapMergeJudge"
DECISIONS_FILE = "state/merge_decisions.json"
MAX_CANDIDATES = 3
JACCARD_FLOOR = 0.25
BATCH_SIZE = 10
SNIPPET_CHARS = 400
ACCEPTED = ("high", "medium")

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "sitemap_id": {"type": "string"},
            "match": {"type": ["string", "null"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["sitemap_id", "match", "confidence", "reason"],
    }}},
    "required": ["decisions"],
}

SYSTEM_PROMPT = ("You match records of European Commission technical support projects (DG REFORM). "
                 "You answer only from the records given.")

INSTRUCTIONS = """Each item below is a project page published on the Commission website, with up to three \
CANDIDATE records taken from listings (TSI country factsheets, the SRSP PDF list of approved requests, policy \
pages). For each item, say which candidate, if any, is the SAME project.

Same project = the same technical support request: same beneficiary country (or institution) and same reform, \
possibly with a reworded or shortened title. Different projects: a different phase or part (phase II, follow-up), \
a different beneficiary country, or a multi-country umbrella project versus a single-country request. When the \
records do not allow a clear answer, return match null or a low confidence.

Return one decision per item: sitemap_id (the item id), match (a candidate id or null), confidence \
(high, medium or low) and a short reason (one sentence)."""


def listing_source(ref: ProjectRef) -> str:
    urls = ref.listing_urls
    if any(u.lower().endswith(".pdf") for u in urls):
        return "SRSP PDF"
    if any("tsi-country-factsheets" in u for u in urls):
        return "factsheet"
    return "policy page"


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _compatible(page: ProjectRef, countries: list[str], t: ProjectRef) -> bool:
    if page.programme and t.programme and page.programme != t.programme:
        return False
    if page.year is not None and t.year is not None and abs(page.year - t.year) > 1:
        return False
    return not (countries and t.countries and not set(countries) & set(t.countries))


def select_candidates(page: ProjectRef, countries: list[str], thins: list[ProjectRef],
                      k: int = MAX_CANDIDATES, floor: float = JACCARD_FLOOR) -> list[tuple[ProjectRef, float]]:
    """Projets thin compatibles les plus proches (Jaccard sur les mots significatifs), au plus k, score >= floor."""
    tokens = prefix_tokens(page.title)
    scored = [(t, round(_jaccard(tokens, prefix_tokens(t.title)), 3)) for t in thins
              if _compatible(page, countries, t)]
    scored = [(t, s) for t, s in scored if s >= floor]
    scored.sort(key=lambda ts: (-ts[1], ts[0].project_id))
    return scored[:k]


def decision_key(slug: str, candidate_ids: list[str]) -> str:
    return f"{slug}|{','.join(sorted(candidate_ids))}"


def _parse_decisions(text: str) -> list[dict[str, Any]]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    data = json.loads(text[start:end + 1])
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("no decisions list")
    return [d for d in decisions if isinstance(d, dict)]


@component
class SitemapMergeJudge:
    """Rattache par LLM les pages du sitemap sans fusion a un projet thin reformule (voir docstring du module)."""

    def __init__(self, chat_generator: Any = None, output_dir: str = "output", cache_dir: str = ".cache/http",
                 min_interval_s: float = 2.0, user_agent: str = DEFAULT_USER_AGENT, batch_size: int = BATCH_SIZE):
        """:param chat_generator: chat generator a sortie structuree (defaut : CLI claude, sonnet, JUDGE_SCHEMA,
        journalise : chaque appel compte dans les couts du run)."""
        self.chat_generator = chat_generator or LoggedChatGenerator(
            chat_generator=ClaudeCliChatGenerator(model="sonnet", json_schema=JUDGE_SCHEMA, timeout_s=300))
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.batch_size = batch_size
        self._fetcher: Any = None

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, chat_generator=component_to_dict(obj=self.chat_generator, name="chat_generator"),
                               output_dir=self.output_dir, cache_dir=self.cache_dir,
                               min_interval_s=self.min_interval_s, user_agent=self.user_agent,
                               batch_size=self.batch_size)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SitemapMergeJudge":
        deserialize_chatgenerator_inplace(data["init_parameters"], key="chat_generator")
        return default_from_dict(cls, data)

    def _get_fetcher(self) -> Any:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    # --- decisions enregistrees --------------------------------------------------------------------------------
    @property
    def _path(self) -> Path:
        return Path(self.output_dir) / DECISIONS_FILE

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8")).get("decisions", {})
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            runlog.event(NAME, "error", str(self._path), "decisions_unreadable", error=repr(exc))
            return {}

    def _save(self, decisions: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps({"decisions": dict(sorted(decisions.items()))}, indent=1, ensure_ascii=False)

        def write() -> None:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)

        _retry_on_lock(write)

    # --- page ----------------------------------------------------------------------------------------------------
    def _snippet(self, url: str) -> str:
        """Debut de l'accroche et de la premiere section de la page (cache HTTP, aucune requete)."""
        try:
            content = self._get_fetcher().cached(url)
        except Exception:  # extrait facultatif
            content = None
        if not content:
            return ""
        parsed = parse_detail(content)
        parts = [parsed.intro] + [body for _, body in parsed.sections[:1]]
        text = " ".join(" ".join(p.split()) for p in parts if p).strip() or " ".join(parsed.text.split())
        return text[:SNIPPET_CHARS]

    @staticmethod
    def _item_text(n: int, page: ProjectRef, countries: list[str], snippet: str,
                   candidates: list[tuple[ProjectRef, float]]) -> str:
        lines = [f"## Item {n}", f"Item id: {page.project_id}", f"Title: {page.title}",
                 f"Programme and year: {page.programme or '?'} {page.year or '?'}",
                 f"Countries (guessed): {', '.join(countries) or 'unknown'}",
                 f"Page text (start): {snippet or '(not available)'}", "Candidates:"]
        for t, _ in candidates:
            lines.append(f"- id: {t.project_id} | title: {t.title} | {t.programme or '?'} {t.year or '?'} | "
                         f"countries: {', '.join(t.countries) or 'unknown'} | listed in: {listing_source(t)}")
        return "\n".join(lines)

    def _ask(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt = INSTRUCTIONS + "\n\n" + "\n\n".join(
            self._item_text(n, it["page"], it["countries"], it["snippet"], it["candidates"])
            for n, it in enumerate(batch, start=1))
        reply = self.chat_generator.run(messages=[ChatMessage.from_system(SYSTEM_PROMPT),
                                                  ChatMessage.from_user(prompt)])["replies"][0]
        return _parse_decisions(reply.text or "")

    # --- run -----------------------------------------------------------------------------------------------------
    @component.output_types(refs=list[ProjectRef], report=dict[str, Any])
    def run(self, refs: list[ProjectRef], report: dict[str, Any] | None = None,
            excluded_thin_ids: list[str] | None = None) -> dict[str, Any]:
        report = dict(report or {})
        excluded = set(excluded_thin_ids or [])
        new_ids = [p["project_id"] for p in report.get("new_projects", [])]
        by_id = {r.project_id: r for r in refs}
        thins = [r for r in refs if not r.detail_url and r.project_id not in excluded]
        summary: dict[str, Any] = {"judged": 0, "llm_calls": 0, "errors": 0, "merged_by_judge": [], "rejected": []}
        items = []
        for slug in new_ids:
            page = by_id.get(slug)
            if page is None or not page.detail_url:
                continue
            snippet = self._snippet(page.detail_url)
            countries = page.countries or countries_in_text(page.title) or countries_in_text(snippet)
            candidates = select_candidates(page, countries, thins)
            if candidates:
                ids = [t.project_id for t, _ in candidates]
                items.append({"page": page, "countries": countries, "snippet": snippet, "candidates": candidates,
                              "key": decision_key(slug, ids)})
        summary["judged"] = len(items)
        stored = self._load()
        todo = [it for it in items if it["key"] not in stored]
        changed = False
        for start in range(0, len(todo), self.batch_size):
            batch = todo[start:start + self.batch_size]
            try:
                answers = {d.get("sitemap_id"): d for d in self._ask(batch)}
                summary["llm_calls"] += 1
            except Exception as exc:  # quota, CLI, reponse illisible : aucune fusion, rien en cache
                summary["errors"] += 1
                runlog.event(NAME, "error", None, "judge_failed", items=len(batch), error=repr(exc))
                continue
            for it in batch:
                ids = [t.project_id for t, _ in it["candidates"]]
                answer = answers.get(it["page"].project_id)
                if answer is None:
                    continue  # pas de reponse pour cet item : redemande au prochain run
                match = answer.get("match")
                stored[it["key"]] = {
                    "sitemap_slug": it["page"].project_id, "candidates": ids,
                    "match": match if match in ids else None,
                    "confidence": answer.get("confidence"), "reason": answer.get("reason", ""),
                    "llm_match": match, "decided_on": date.today().isoformat()}
                changed = True
        if changed:
            try:
                self._save(stored)
            except OSError as exc:
                runlog.event(NAME, "error", str(self._path), "decisions_not_saved", error=repr(exc))
        refs, merged_slugs = self._apply(refs, items, stored, summary, fresh={it["key"] for it in todo})
        report["new_projects"] = [p for p in report.get("new_projects", []) if p["project_id"] not in merged_slugs]
        report["refs"] = len(refs)
        report["judge"] = summary
        return {"refs": refs, "report": report}

    def _apply(self, refs: list[ProjectRef], items: list[dict[str, Any]], stored: dict[str, dict[str, Any]],
               summary: dict[str, Any], fresh: set[str]) -> tuple[list[ProjectRef], set[str]]:
        index = {r.project_id: i for i, r in enumerate(refs)}
        out = list(refs)
        absorbed: set[str] = set()
        merged_slugs: set[str] = set()
        for it in items:
            slug = it["page"].project_id
            ids = [t.project_id for t, _ in it["candidates"]]
            entry = stored.get(it["key"])
            if entry is None:
                continue
            override = entry.get("override")
            if override is not None:
                match, confidence = (None if override == "none" else override), "override"
            else:
                match, confidence = entry.get("match"), entry.get("confidence")
            cached = it["key"] not in fresh
            reason = entry.get("reason", "")
            accepted = match is not None and (confidence == "override" or confidence in ACCEPTED)
            if accepted and (match in absorbed or match not in index):
                accepted, reason = False, f"{match} already merged or unknown; {reason}"
            runlog.event(NAME, "merge_judge", slug, "merged" if accepted else "rejected", match=match,
                         confidence=confidence, reason=reason, candidates=ids, cached=cached)
            if not accepted:
                summary["rejected"].append({"sitemap_slug": slug, "candidates": ids, "match": match,
                                            "confidence": confidence, "reason": reason, "cached": cached})
                continue
            i, j = index[slug], index[match]
            out[i] = combine(out[i], out[j])
            absorbed.add(match)
            merged_slugs.add(slug)
            summary["merged_by_judge"].append({"sitemap_slug": slug, "thin_id": match, "confidence": confidence,
                                               "reason": reason, "cached": cached})
        return [r for r in out if r.project_id not in absorbed or r.detail_url], merged_slugs
