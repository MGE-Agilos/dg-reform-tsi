"""Banc d'agregation : le chatbot retrouve-t-il TOUS les pays / projets d'une question globale ?

Deux familles de questions :
- metadonnees (domaine, pays) : verite exacte tiree du catalogue des projets (front matter) ;
- themes libres (cancer, hydrogene...) : candidats = mots-cles du titre + projets des 300 premiers morceaux
  semantiques, puis un juge LLM decide projet par projet ; la verite est enregistree et reutilisee.
Chaque question passe par le vrai pipeline du chatbot (`ask`, sans historique) ; on mesure rappel, precision et F1
des pays cites dans le texte de la reponse, ou des projets cites (sources + titres/ids), ou l'ecart du comptage.
"""

import json
import random
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.chat.service import ask
from tsi_agent.chat.tools import build_filters
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.countries import COUNTRIES, FRENCH_NAMES, name_from_iso
from tsi_agent.impact.catalog import load_catalog

JUDGE_MODEL = "sonnet"
TOP_CHUNKS = 300
JUDGE_BATCH = 25
EXCERPT_CHARS = 600
MIN_TITLE_CHARS = 15
TRUTH_FILE = "aggregation_truth.json"
ALWAYS_AREAS = ("health-and-long-term-care",)

AREAS: dict[str, tuple[str, str]] = {
    "competitiveness": ("competitiveness", "compétitivité"),
    "digital-transition": ("digital transition", "transition numérique"),
    "financial-sector-and-access-finance": ("financial sector and access to finance",
                                            "secteur financier et accès au financement"),
    "green-transition": ("green transition", "transition verte"),
    "health-and-long-term-care": ("health and long-term care", "santé et soins de longue durée"),
    "labour-market-and-social-protection": ("labour market and social protection",
                                            "marché du travail et protection sociale"),
    "migration-management": ("migration management", "gestion des migrations"),
    "public-administration-and-governance": ("public administration and governance",
                                             "administration publique et gouvernance"),
    "revenue-administration-and-public-financial-management": (
        "revenue administration and public financial management",
        "administration fiscale et gestion des finances publiques"),
    "skills-education-and-training": ("skills, education and training", "compétences, éducation et formation"),
    "recovery-and-resilience-plans": ("recovery and resilience plans", "plans pour la reprise et la résilience"),
}

DEFAULT_TOPICS: list[dict[str, Any]] = [
    {"id": "cancer", "label_en": "cancer screening or cancer care", "label_fr": "le dépistage ou la prise en charge du cancer",
     "query": "cancer screening, cancer care and oncology", "keywords": ["cancer", "oncolog"]},
    {"id": "hydrogen", "label_en": "hydrogen", "label_fr": "l'hydrogène",
     "query": "hydrogen strategy, hydrogen production and use", "keywords": ["hydrogen"]},
    {"id": "wildfires", "label_en": "wildfires or forest fires", "label_fr": "les feux de forêt",
     "query": "wildfire and forest fire prevention and management", "keywords": ["wildfire", "forest fire", "fire"]},
    {"id": "aml", "label_en": "anti-money laundering", "label_fr": "la lutte contre le blanchiment de capitaux",
     "query": "anti-money laundering and countering the financing of terrorism",
     "keywords": ["money laundering", "anti-money", "aml", "financing of terrorism"]},
    {"id": "cybersecurity", "label_en": "cybersecurity", "label_fr": "la cybersécurité",
     "query": "cybersecurity and cyber resilience", "keywords": ["cyber"]},
    {"id": "pensions", "label_en": "pension reform", "label_fr": "la réforme des retraites",
     "query": "pension system reform and pension adequacy", "keywords": ["pension", "retirement"]},
    {"id": "migrants", "label_en": "the integration of migrants", "label_fr": "l'intégration des migrants",
     "query": "integration of migrants, refugees and third-country nationals",
     "keywords": ["migrant", "refugee", "third-country national"]},
    {"id": "justice", "label_en": "the digitalisation of courts and justice",
     "label_fr": "la numérisation des tribunaux et de la justice",
     "query": "digitalisation of courts and the justice system, e-justice",
     "keywords": ["court", "justice", "judicia", "e-justice"]},
]

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {"ref": {"type": "integer"}, "relevant": {"type": "boolean"}},
        "required": ["ref", "relevant"]}}},
    "required": ["decisions"],
}
JUDGE_SYSTEM = ("You classify EU technical support projects (TSI / SRSP, DG REFORM) by topic, strictly and "
                "consistently.")
JUDGE_INSTRUCTIONS = """Topic: {topic}

For each numbered project below, decide whether the project substantially deals with this topic (the topic is a main subject or an explicit component of the support, not a passing mention or a vague link). Return one decision per project, with `ref` set to the project number."""


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def readable_area(slug: str, lang: str = "en") -> str:
    names = AREAS.get(slug)
    if names is None:
        return slug.replace("-", " ")
    return names[1] if lang == "fr" else names[0]


def _country_name(iso: str, lang: str) -> str:
    return FRENCH_NAMES[iso][0] if lang == "fr" and iso in FRENCH_NAMES else name_from_iso(iso)


# --- questions sur les metadonnees (verite exacte) ---

def _lang(index: int) -> str:
    return "en" if index % 2 == 0 else "fr"


def _question(qid: str, kind: str, lang: str, text: str, filters: dict[str, Any], *, countries=(), projects=(),
              count: int | None = None, source: str = "metadata", topic: str | None = None) -> dict[str, Any]:
    return {"id": qid, "source": source, "kind": kind, "lang": lang, "question": text, "filters": filters,
            "truth_countries": sorted(set(countries)), "truth_projects": sorted(set(projects)), "truth_count": count,
            "topic": topic}


def metadata_questions(catalog: list[dict[str, Any]], seed: int = 7, areas: list[str] | None = None,
                       n_areas: int = 6, n_lists: int = 3, n_counts: int = 3,
                       include_areas: tuple[str, ...] = ALWAYS_AREAS) -> list[dict[str, Any]]:
    """Questions pays par domaine, liste de projets et comptage pour des couples (pays, domaine) ; tirage reproductible.

    Les domaines de `include_areas` (sante : l'exemple d'origine) sont toujours interroges quand ils existent.
    """
    rng = random.Random(seed)
    by_area: dict[str, list[dict[str, Any]]] = {}
    for p in catalog:
        area = p.get("policy_area")
        if area and (areas is None or area in areas):
            by_area.setdefault(area, []).append(p)
    pairs: dict[tuple[str, str], list[str]] = {}
    for area, projects in by_area.items():
        for p in projects:
            for c in p.get("countries") or []:
                if c in COUNTRIES:
                    pairs.setdefault((c, area), []).append(p["project_id"])
    questions: list[dict[str, Any]] = []

    def add(kind: str, make: Callable[[str], str], filters: dict[str, Any], **truth: Any) -> None:
        lang = _lang(len(questions))
        questions.append(_question(f"m{len(questions) + 1:02d}", kind, lang, make(lang), filters, **truth))

    forced = [a for a in include_areas if a in by_area][:n_areas]
    others = sorted(a for a in by_area if a not in forced)
    for area in forced + rng.sample(others, min(n_areas - len(forced), len(others))):
        countries = {c for p in by_area[area] for c in p.get("countries") or []}
        add("countries", lambda lang, a=area: (
            f"Which countries have TSI or SRSP projects in {readable_area(a)}?" if lang == "en" else
            f"Quels pays ont des projets TSI ou SRSP dans le domaine « {readable_area(a, 'fr')} » ?"),
            {"policy_area": area}, countries=countries)
    eligible = sorted(k for k, ids in pairs.items() if 3 <= len(ids) <= 15)
    picked = rng.sample(eligible, min(n_lists, len(eligible)))
    for country, area in picked:
        add("projects", lambda lang, c=country, a=area: (
            f"List the projects on {readable_area(a)} in {_country_name(c, 'en')}." if lang == "en" else
            f"Liste les projets du domaine « {readable_area(a, 'fr')} » (pays : {_country_name(c, 'fr')})."),
            {"policy_area": area, "country": country}, projects=pairs[(country, area)])
    rest = sorted(k for k in pairs if k not in picked)
    for country, area in rng.sample(rest, min(n_counts, len(rest))):
        add("count", lambda lang, c=country, a=area: (
            f"How many projects does {_country_name(c, 'en')} have in {readable_area(a)}?" if lang == "en" else
            f"Combien de projets dans le domaine « {readable_area(a, 'fr')} » pour le pays "
            f"{_country_name(c, 'fr')} ?"),
            {"policy_area": area, "country": country}, count=len(pairs[(country, area)]))
    return questions


# --- themes libres : candidats, juge, verite ---

def _sections(body: str) -> tuple[str, dict[str, str]]:
    """Introduction (texte entre le titre H1 et la premiere section) et sections `## ...` du corps."""
    intro: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] = intro
    for line in body.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif not line.startswith("# "):
            current.append(line)
    return " ".join(" ".join(intro).split()), {k: " ".join(" ".join(v).split()) for k, v in sections.items()}


def load_project_texts(projects_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Par projet : titres des listes officielles et extrait (resume IA, sinon introduction, sinon contexte)."""
    texts: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(projects_dir).glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
            _, fm_text, body = raw.split("---\n", 2) if raw.startswith("---\n") else ("", "", raw)
            fm = yaml.safe_load(fm_text) or {}
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(fm, dict) or not fm.get("project_id"):
            continue
        intro, sections = _sections(body)
        excerpt = sections.get("Summary (AI-generated)") or intro or sections.get("Context") or ""
        texts[fm["project_id"]] = {"listing_titles": list(fm.get("listing_titles") or []),
                                   "excerpt": excerpt[:EXCERPT_CHARS]}
    return texts


def _keyword_hit(text: str, keywords: list[str]) -> bool:
    folded = _fold(text)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(_fold(k)), folded) for k in keywords)


def topic_candidates(topic: dict[str, Any], catalog: list[dict[str, Any]], texts: dict[str, dict[str, Any]],
                     store: Any, text_embedder: Any, retriever: Any = None,
                     top_chunks: int = TOP_CHUNKS) -> list[str]:
    """Projets dont le titre (ou un titre de liste) contient un mot-cle, puis projets des morceaux semantiques."""
    known = {p["project_id"] for p in catalog}
    found: dict[str, None] = {}
    for p in catalog:
        listing = " ".join(texts.get(p["project_id"], {}).get("listing_titles") or [])
        if _keyword_hit(f"{p.get('title') or ''} {listing}", topic.get("keywords") or []):
            found[p["project_id"]] = None
    if top_chunks > 0:
        if retriever is None:
            from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever
            retriever = LocalQdrantEmbeddingRetriever(document_store=store)
        embedding = text_embedder.run(text=topic["query"])["embedding"]
        docs = retriever.run(query_embedding=embedding, top_k=top_chunks,
                             filters=build_filters(doc_type=["project", "deliverable"]))["documents"]
        for doc in docs:
            ids = [doc.meta.get("doc_id")] if doc.meta.get("doc_type") == "project" else doc.meta.get("projects") or []
            for pid in ids:
                if pid in known:
                    found[pid] = None
    return list(found)


def _judge_prompt(topic: dict[str, Any], batch: list[dict[str, Any]], texts: dict[str, dict[str, Any]]) -> str:
    parts = [JUDGE_INSTRUCTIONS.format(topic=topic["label_en"])]
    for ref, p in enumerate(batch, start=1):
        head = " — ".join(str(v) for v in (p.get("programme"), p.get("year"), ", ".join(p.get("countries") or []))
                          if v)
        parts.append(f"[{ref}] id: {p['project_id']}\nTitle: {p.get('title')} ({head})\n"
                     f"{texts.get(p['project_id'], {}).get('excerpt') or '(no description)'}")
    return "\n\n".join(parts)


def _parse_decisions(text: str) -> list[dict[str, Any]]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    items = json.loads(text[start:end + 1]).get("decisions")
    if not isinstance(items, list):
        raise ValueError("reply has no `decisions` list")
    return [i for i in items if isinstance(i, dict)]


def judge_candidates(topic: dict[str, Any], candidates: list[str], catalog: list[dict[str, Any]],
                     texts: dict[str, dict[str, Any]], judge: Any,
                     batch_size: int = JUDGE_BATCH) -> tuple[list[str], int]:
    """Projets juges pertinents (tries) et nombre de candidats non juges (lot illisible deux fois)."""
    by_id = {p["project_id"]: p for p in catalog}
    projects = [by_id[c] for c in candidates if c in by_id]
    relevant: set[str] = set()
    unjudged = 0
    for start in range(0, len(projects), batch_size):
        batch = projects[start:start + batch_size]
        messages = [ChatMessage.from_system(JUDGE_SYSTEM), ChatMessage.from_user(_judge_prompt(topic, batch, texts))]
        decisions = None
        for attempt in range(2):
            try:
                decisions = _parse_decisions(judge.run(messages=messages)["replies"][0].text or "")
                break
            except LLMQuotaExceeded:
                raise
            except Exception as exc:  # reponse illisible ou erreur du CLI : une nouvelle tentative
                runlog.event("judge_candidates", "alert", topic["id"], "warning",
                             message=f"judge batch failed (attempt {attempt + 1}): {exc!r}"[:300])
        if decisions is None:
            unjudged += len(batch)
            continue
        seen = set()
        for d in decisions:
            ref = d.get("ref")
            if isinstance(ref, int) and 1 <= ref <= len(batch) and ref not in seen:
                seen.add(ref)
                if d.get("relevant") is True:
                    relevant.add(batch[ref - 1]["project_id"])
        unjudged += len(batch) - len(seen)
    return sorted(relevant), unjudged


def build_topic_truth(topics: list[dict[str, Any]], catalog: list[dict[str, Any]], store: Any, text_embedder: Any,
                      judge: Any, projects_dir: str | Path, *, retriever: Any = None, top_chunks: int = TOP_CHUNKS,
                      batch_size: int = JUDGE_BATCH,
                      progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    say = progress or (lambda _msg: None)
    if hasattr(text_embedder, "warm_up"):
        text_embedder.warm_up()
    texts = load_project_texts(projects_dir)
    by_id = {p["project_id"]: p for p in catalog}
    truth = []
    for topic in topics:
        candidates = topic_candidates(topic, catalog, texts, store, text_embedder, retriever, top_chunks)
        say(f"Thème {topic['id']} : {len(candidates)} projets candidats, jugement par lots de {batch_size}...")
        relevant, unjudged = judge_candidates(topic, candidates, catalog, texts, judge, batch_size)
        countries = sorted({c for pid in relevant for c in by_id[pid].get("countries") or []})
        say(f"  {len(relevant)} projets pertinents, {len(countries)} pays" + (f", {unjudged} non jugés" if unjudged
                                                                               else ""))
        truth.append({**topic, "candidates": candidates, "relevant": relevant, "countries": countries,
                      "unjudged": unjudged})
    return truth


def questions_from_truth(truth: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deux questions par theme (pays, projets), langues alternees."""
    questions = []
    for i, t in enumerate(truth):
        lang_countries, lang_projects = ("en", "fr") if i % 2 == 0 else ("fr", "en")
        text = (f"Which countries have projects on {t['label_en']}?" if lang_countries == "en"
                else f"Quels pays ont des projets sur {t['label_fr']} ?")
        questions.append(_question(f"t{len(questions) + 1:02d}", "countries", lang_countries, text, {},
                                   countries=t["countries"], source="topic", topic=t["id"]))
        text = (f"Which projects deal with {t['label_en']}?" if lang_projects == "en"
                else f"Quels projets portent sur {t['label_fr']} ?")
        questions.append(_question(f"t{len(questions) + 1:02d}", "projects", lang_projects, text, {},
                                   projects=t["relevant"], source="topic", topic=t["id"]))
    return questions


def topic_questions(topics: list[dict[str, Any]], catalog: list[dict[str, Any]], store: Any, text_embedder: Any,
                    judge: Any, projects_dir: str | Path, **kw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    truth = build_topic_truth(topics, catalog, store, text_embedder, judge, projects_dir, **kw)
    return questions_from_truth(truth), truth


# --- lecture d'une reponse ---

def _country_patterns() -> list[tuple[re.Pattern[str], str]]:
    names = [(n, iso) for iso, (_, en) in COUNTRIES.items() for n in en]
    names += [(n, iso) for iso, fr in FRENCH_NAMES.items() for n in fr]
    return [(re.compile(r"(?<![a-z0-9])" + re.escape(_fold(n)) + r"(?![a-z0-9])"), iso) for n, iso in names]


_NAME_PATTERNS = _country_patterns()
# Codes ISO : majuscules, mot entier ; EL = Grece (code UE). "IT" suivi d'un mot en minuscules ou d'un tiret
# ("IT systems", "IT-tools") est l'informatique, pas l'Italie.
_CODES = {**{iso: iso for iso in COUNTRIES}, "EL": "GR"}
_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9-])(" + "|".join(sorted(_CODES)) + r")(?![A-Za-z0-9])")


def detect_countries(text: str) -> set[str]:
    """Pays cites dans le texte : noms anglais/francais (casse et accents ignores) et codes ISO en majuscules."""
    folded = _fold(text)
    found = {iso for pattern, iso in _NAME_PATTERNS if pattern.search(folded)}
    for m in _CODE_PATTERN.finditer(text or ""):
        after = text[m.end():m.end() + 2]
        if m.group(1) == "IT" and (after.startswith("-") or re.match(r"\s[a-z]", after)):
            continue
        found.add(_CODES[m.group(1)])
    return found


def projects_mentioned(answer: str, sources: list[dict[str, Any]], catalog: list[dict[str, Any]]) -> set[str]:
    """Projets cites : doc_id des sources citees, titres de projets recopies (casse ignoree) et ids ecrits tels quels."""
    found = {s["doc_id"] for s in sources or [] if s.get("doc_id")}
    folded = _fold(answer)
    for p in catalog:
        title = _fold(str(p.get("title") or ""))
        pid = str(p.get("project_id") or "")
        if len(title) >= MIN_TITLE_CHARS and title in folded:
            found.add(pid)
        elif "-" in pid and re.search(r"(?<![\w-])" + re.escape(pid.lower()) + r"(?![\w-])", folded):
            found.add(pid)
    return found


def parse_count(text: str) -> int | None:
    """Premier entier de la reponse, hors renvois [n] et annees (1990-2100)."""
    cleaned = re.sub(r"\[\d+\]", " ", text or "")
    for m in re.finditer(r"(?<![\w.,-])\d+(?![\w])", cleaned):
        value = int(m.group())
        if not 1990 <= value <= 2100:
            return value
    return None


def set_metrics(found: set[str], truth: set[str]) -> dict[str, Any]:
    hit = len(found & truth)
    recall = hit / len(truth) if truth else None
    precision = hit / len(found) if found else None
    f1 = 2 * recall * precision / (recall + precision) if recall and precision else 0.0
    return {"recall": recall, "precision": precision, "f1": f1, "missed": sorted(truth - found),
            "extra": sorted(found - truth)}


def score_answer(question: dict[str, Any], result: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    answer = result.get("answer") or ""
    if question["kind"] == "count":
        predicted = parse_count(answer)
        truth = question["truth_count"]
        return {"predicted_count": predicted, "abs_error": None if predicted is None else abs(predicted - truth),
                "exact": predicted == truth}
    if question["kind"] == "countries":
        found = detect_countries(answer)
        truth = set(question["truth_countries"])
    else:
        found = projects_mentioned(answer, result.get("sources") or [], catalog)
        truth = set(question["truth_projects"])
    return {"found": sorted(found), **set_metrics(found, truth)}


def run_aggregation(settings: Settings, pipeline: Any, questions: list[dict[str, Any]], ask_fn: Callable = ask,
                    catalog: list[dict[str, Any]] | None = None,
                    progress: Callable[[str], None] | None = print) -> list[dict[str, Any]]:
    """Chaque question passe par le chatbot (sans historique) ; une ligne de resultats par question."""
    say = progress or (lambda _msg: None)
    if catalog is None:
        catalog = load_catalog(Path(settings.output_dir) / "projects")
    rows = []
    for n, q in enumerate(questions, start=1):
        t0 = time.monotonic()
        row: dict[str, Any] = {**q, "answer": None, "error": None, "steps": [], "tools": [], "found": [],
                               "recall": None, "precision": None, "f1": None, "missed": [], "extra": [],
                               "predicted_count": None, "abs_error": None, "exact": None, "cost_usd": 0.0}
        try:
            result = ask_fn(pipeline, q["question"], [], logs_dir=settings.logs_dir)
        except LLMQuotaExceeded:
            raise
        except Exception as exc:  # une question en echec n'arrete pas le banc
            row.update(error=repr(exc)[:300], duration_s=round(time.monotonic() - t0, 1))
            rows.append(row)
            say(f"  {n}/{len(questions)} {q['id']} ERREUR : {exc}")
            continue
        steps = [{"tool": s.get("tool"), "arguments": s.get("arguments")} for s in result.get("steps") or []]
        row.update(answer=result.get("answer"), steps=steps, tools=[s["tool"] for s in steps],
                   sources=[s.get("doc_id") for s in result.get("sources") or []],
                   duration_s=result.get("duration_s"), cost_usd=result.get("cost_usd") or 0.0,
                   run_id=result.get("run_id"), **score_answer(q, result, catalog))
        rows.append(row)
        say(f"  {n}/{len(questions)} {q['id']} ({q['kind']}) : "
            + (f"compte {row['predicted_count']} / {q['truth_count']}" if q["kind"] == "count"
               else f"rappel {row['recall'] if row['recall'] is None else round(row['recall'], 2)}"))
    return rows


# --- commande ---

def default_judge(settings: Settings) -> Any:
    from tsi_agent.chat.generator import LoggedChatGenerator
    from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
    return LoggedChatGenerator(ClaudeCliChatGenerator(model=JUDGE_MODEL, json_schema=JUDGE_SCHEMA, timeout_s=300),
                               pricing=settings.pricing)


def run_eval_aggregation(settings: Settings, *, out_dir: str | Path = "output/eval", regenerate: bool = False,
                         topics_only: bool = False, metadata_only: bool = False,
                         policy_areas: list[str] | None = None, topics: list[dict[str, Any]] | None = None,
                         store: Any = None, text_embedder: Any = None, judge: Any = None, pipeline: Any = None,
                         chat_generator: Any = None, ask_fn: Callable = ask, seed: int = 7, today: str | None = None,
                         run_id: str | None = None,
                         progress: Callable[[str], None] | None = print) -> dict[str, Any]:
    from tsi_agent.eval import aggregation_report
    from tsi_agent.eval.runner import open_eval_store
    say = progress or (lambda _msg: None)
    store = open_eval_store(settings, store)
    projects_dir = Path(settings.output_dir) / "projects"
    catalog = load_catalog(projects_dir)
    if not catalog:
        raise RuntimeError(f"catalogue des projets vide ({projects_dir}) : lancez d'abord python -m tsi_agent run")
    out_dir = Path(out_dir)
    truth_path = out_dir / TRUTH_FILE
    today = today or date.today().isoformat()
    if text_embedder is None:
        from tsi_agent.chat.pipeline import default_text_embedder
        text_embedder = default_text_embedder(settings)
    with runlog.open_run(settings.logs_dir, run_id) as run:
        questions = [] if topics_only else metadata_questions(catalog, seed=seed, areas=policy_areas or None)
        truth: list[dict[str, Any]] = []
        if not metadata_only:
            if truth_path.exists() and not regenerate:
                truth = json.loads(truth_path.read_text(encoding="utf-8"))["topics"]
                say(f"Vérité des thèmes réutilisée : {truth_path} ({len(truth)} thèmes)")
            else:
                truth = build_topic_truth(topics or DEFAULT_TOPICS, catalog, store, text_embedder,
                                          judge or default_judge(settings), projects_dir, progress=say)
                out_dir.mkdir(parents=True, exist_ok=True)
                truth_path.write_text(json.dumps({"date": today, "judge_model": JUDGE_MODEL, "topics": truth},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
                say(f"Vérité des thèmes enregistrée : {truth_path}")
            questions += questions_from_truth(truth)
        if pipeline is None:
            from tsi_agent.chat.pipeline import build_chat_pipeline
            pipeline = build_chat_pipeline(settings, policy_areas or [], chat_generator=chat_generator,
                                           text_embedder=text_embedder, document_store=store)
        say(f"{len(questions)} questions au chatbot ({settings.chat_model})...")
        rows = run_aggregation(settings, pipeline, questions, ask_fn=ask_fn, catalog=catalog, progress=say)
        setup = {"date": today, "chat_model": settings.chat_model, "judge_model": JUDGE_MODEL,
                 "truth_file": str(truth_path) if truth else None, "catalog_size": len(catalog),
                 "topics": [{"id": t["id"], "label": t.get("label_en"), "candidates": len(t.get("candidates") or []),
                             "relevant": t.get("relevant") or [], "countries": t.get("countries") or [],
                             "unjudged": t.get("unjudged", 0)} for t in truth]}
        md_path, csv_path = out_dir / f"aggregation-{today}.md", out_dir / f"aggregation-{today}.csv"
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(aggregation_report.render_report(rows, setup), encoding="utf-8")
        aggregation_report.write_csv(rows, csv_path)
        run.extra.update({"mode": "eval_aggregation", "questions": len(questions), "report": str(md_path)})
    summary = run.final_summary or {}
    chat_cost = sum(r.get("cost_usd") or 0.0 for r in rows)
    return {"report": str(md_path), "csv": str(csv_path), "truth_file": str(truth_path) if truth else None,
            "questions": len(questions), "cost_usd": round(summary.get("cost_usd", 0.0) + chat_cost, 4),
            "run_id": run.run_id}
