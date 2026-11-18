"""Domaine politique des projets qui n'en ont pas, par LLM (CLI Claude ou OpenAI, sortie JSON), en lot avec cache.

Le domaine officiel d'un projet vient de l'URL de sa page detail (rubrique du site). Les projets connus seulement par
une factsheet pays ou le PDF SRSP (environ 1 300) n'en ont pas : ils apparaissaient « Not classified » dans
l'explorateur. Ici, le LLM choisit l'un des domaines du site d'apres le titre (et le resume quand il existe), ou
« unclear ». Le resultat est ecrit dans le front matter (`policy_area`, `policy_area_source: ai`) ; un domaine officiel
n'est jamais remplace. Decisions dans output/project_areas.json, rejouees sans LLM tant que l'entree (version, titres,
resume) ne change pas : `run` peut reecrire le front matter d'un projet thin, l'etape suivante le remet.
"""

import contextvars
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.chunking import INTRO, SUMMARY, split_sections
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.countries import name_from_iso
from tsi_agent.render import parse_front_matter, render
from tsi_agent.store import _retry_on_lock

PROMPT_VERSION = 1
BATCH = 40
SUMMARY_CHARS = 600
RECORDS_FILE = "project_areas.json"
SOURCE_AI = "ai"
UNCLEAR = "unclear"
NAME = "AreaClassifier"

# Domaines du site (config/sources.yaml, policy_areas) et ce qu'ils couvrent, pour guider le choix.
AREAS: dict[str, str] = {
    "competitiveness": "business environment, SMEs, investment climate, industrial and innovation policy, research, "
                       "competition, state aid, public procurement, trade, tourism, agriculture and fisheries, "
                       "transport and logistics, construction and housing markets",
    "digital-transition": "digital public services, e-government, interoperability, data, cybersecurity, "
                          "artificial intelligence, digital skills strategies, broadband",
    "financial-sector-and-access-finance": "banking and insurance supervision, capital markets, financial literacy, "
                                           "non-performing loans, insolvency, anti-money laundering, promotional "
                                           "banks, access to finance, pensions funds supervision",
    "green-transition": "climate mitigation and adaptation, energy (markets, regulation, efficiency, renewables), "
                        "environment, water, waste and circular economy, biodiversity, wildfires and natural "
                        "disasters, sustainable mobility, just transition, renovation wave",
    "health-and-long-term-care": "health systems, hospitals, health workforce, e-health, pharmaceuticals, public "
                                 "health, long-term care",
    "labour-market-and-social-protection": "employment services, active labour market policies, undeclared work, "
                                           "social inclusion, social benefits, pensions systems, social security, "
                                           "gender equality, child guarantee, disability",
    "migration-management": "migration, asylum, reception and integration of migrants or refugees, border management",
    "public-administration-and-governance": "public administration reform, civil service, justice and courts, "
                                            "anti-corruption, rule of law, better regulation, multilevel and local "
                                            "governance, statistics, security and defence governance, policy "
                                            "coherence and sustainable development goals, implementation of EU funds "
                                            "and recovery plans",
    "revenue-administration-and-public-financial-management": "tax policy and tax administration, customs, "
                                                              "budgeting, spending reviews, public debt, fiscal "
                                                              "frameworks, public accounting and audit, state-owned "
                                                              "enterprises and public assets",
    "skills-education-and-training": "education systems, schools, vocational education and training, higher "
                                     "education, adult learning, skills strategies",
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "policy_area": {"type": "string", "enum": [*AREAS, UNCLEAR]}},
        "required": ["id", "policy_area"]}}},
    "required": ["items"],
}

SYSTEM_PROMPT = ("You classify European Commission technical support projects (DG REFORM: TSI, SRSP, SRSS) into the "
                 "policy areas of the DG REFORM website. You answer only from the texts given.")


def instructions() -> str:
    areas = "\n".join(f"- {key}: {desc}" for key, desc in AREAS.items())
    return f"""Give each project below the ONE policy area that best matches its main objective.

Policy areas (key: what it covers):
{areas}

- Decide from the project's title (and summary when given); classify by the project's own objective, not by its \
beneficiary institution alone.
- Answer "{UNCLEAR}" only when the title gives no usable clue (for example a bare acronym).
- Return one item per project, with its id exactly as written."""


def eligible(fm: dict) -> bool:
    """Sans domaine officiel : aucun domaine, ou un domaine deja donne par le LLM."""
    return not fm.get("policy_area") or fm.get("policy_area_source") == SOURCE_AI


def project_input(fm: dict, body: str) -> str:
    """Ligne d'entree d'un projet : titre, autres titres de listing, programme, annee, pays, debut du resume."""
    titles = [t for t in dict.fromkeys(fm.get("listing_titles") or []) if t and t != fm.get("title")]
    parts = [f"Title: {fm.get('title')}"]
    if titles:
        parts.append(f"also listed as: {'; '.join(titles)}")
    parts.append(" ".join(str(v) for v in (fm.get("programme"), fm.get("year")) if v))
    if fm.get("countries"):
        parts.append(", ".join(name_from_iso(c) for c in fm["countries"]))
    if fm.get("detail_page"):
        sections = dict(split_sections(body))
        summary = " ".join(str(sections.get(SUMMARY) or sections.get(INTRO) or "").split())
        if summary:
            parts.append(f"summary: {summary[:SUMMARY_CHARS]}")
    return " | ".join(parts)


def input_hash(text: str) -> str:
    return "sha1:" + hashlib.sha1(f"{PROMPT_VERSION}|{text}".encode("utf-8")).hexdigest()


def build_prompt(items: list[tuple[str, str]]) -> str:
    lines = [f"- id {pid}: {text}" for pid, text in items]
    return "\n\n".join([instructions(), "## Projects", "\n".join(lines)])


def parse_reply(text: str, ids: set[str]) -> dict[str, str]:
    """{id: domaine} des elements valides (id demande, domaine connu) ; le premier l'emporte."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    raw = json.loads(text[start:end + 1]).get("items")
    if not isinstance(raw, list):
        raise ValueError("no items list")
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid, area = item.get("id"), item.get("policy_area")
        if pid in ids and pid not in out and (area in AREAS or area == UNCLEAR):
            out[pid] = area
    return out


def apply_area(text: str, record: dict[str, Any] | None) -> str:
    """Texte du fichier projet avec le domaine du LLM (ou sans, s'il est retire) ; domaine officiel jamais touche."""
    fm, body = parse_front_matter(text.replace("\r\n", "\n"))
    if not eligible(fm):
        return render(fm, body)
    area = (record or {}).get("policy_area")
    out: dict[str, Any] = {}
    for key, value in fm.items():
        if key == "policy_area_source":
            continue
        if key == "policy_area":
            value = area if area in AREAS else None
            out[key] = value
            if value:
                out["policy_area_source"] = SOURCE_AI
            continue
        out[key] = value
    if "policy_area" not in out and area in AREAS:
        out["policy_area"], out["policy_area_source"] = area, SOURCE_AI
    return render(out, body)


def _is_quota(exc: BaseException) -> bool:
    return isinstance(exc, LLMQuotaExceeded) or "RateLimit" in type(exc).__name__


def _read_records(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    def write() -> None:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    _retry_on_lock(write)


def run_classification(settings: Settings, generator: Any, *, limit: int | None = None,
                       concurrency: int | None = None) -> dict[str, Any]:
    """Classe les projets sans domaine officiel (nouveaux ou modifies), puis ecrit le domaine de tous les projets
    classes. S'arrete proprement sur quota (relancer reprend) ; un lot illisible (2 essais) est saute."""
    root = Path(settings.output_dir)
    records_path = root / RECORDS_FILE
    records = _read_records(records_path)
    summary: dict[str, Any] = {"eligible": 0, "classified": 0, "cached": 0, "unclear": 0, "failed": 0,
                               "files_updated": 0, "quota_exceeded": False}
    todo: list[tuple[str, str, str]] = []  # (id, texte, hash)
    files: list[tuple[Path, str]] = []
    for path in sorted((root / "projects").glob("*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except (ValueError, OSError):
            continue
        if not isinstance(fm, dict) or not fm.get("project_id") or not eligible(fm):
            continue
        pid = fm["project_id"]
        files.append((path, pid))
        summary["eligible"] += 1
        text = project_input(fm, body)
        h = input_hash(text)
        if (records.get(pid) or {}).get("input_hash") == h:
            summary["cached"] += 1
            continue
        todo.append((pid, text, h))
    if limit is not None:
        todo = todo[:limit]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    stop = threading.Event()
    lock = threading.Lock()
    model_name = None

    def process(batch: list[tuple[str, str, str]]) -> None:
        nonlocal model_name
        if stop.is_set():
            return
        ids = {pid for pid, _, _ in batch}
        prompt = build_prompt([(pid, text) for pid, text, _ in batch])
        for attempt in (1, 2):
            try:
                reply = generator.run(messages=[ChatMessage.from_system(SYSTEM_PROMPT),
                                                ChatMessage.from_user(prompt)])["replies"][0]
                found = parse_reply(reply.text or "", ids)
                break
            except ValueError as exc:
                if attempt == 2:
                    runlog.event(NAME, "error", batch[0][0], "bad_reply", error=repr(exc)[:300])
                    with lock:
                        summary["failed"] += len(batch)
                    return
            except Exception as exc:
                if _is_quota(exc):
                    stop.set()
                    runlog.event(NAME, "error", batch[0][0], "quota_exceeded", error=str(exc)[:300])
                else:
                    runlog.event(NAME, "error", batch[0][0], "llm_failed", error=repr(exc)[:300])
                    with lock:
                        summary["failed"] += len(batch)
                return
        model = (reply.meta or {}).get("model")
        today = date.today().isoformat()
        with lock:
            model_name = model_name or model
            for pid, _, h in batch:
                if pid not in found:  # oublie par le LLM : redemande au prochain run
                    summary["failed"] += 1
                    continue
                records[pid] = {"policy_area": found[pid], "input_hash": h, "model": model, "date": today}
                summary["unclear" if found[pid] == UNCLEAR else "classified"] += 1
            _write_text(records_path, json.dumps(records, indent=1, ensure_ascii=False, sort_keys=True))
        runlog.event(NAME, "classify", batch[0][0], "ok", projects=len(batch))

    with ThreadPoolExecutor(max_workers=max(1, concurrency or settings.max_concurrency)) as pool:
        # contexte copie : le journal du run (ContextVar) n'est pas herite par les threads du pool
        list(pool.map(lambda b: contextvars.copy_context().run(process, b), batches))
    summary["quota_exceeded"] = stop.is_set()
    for path, pid in files:
        try:
            text = path.read_text(encoding="utf-8")
            new = apply_area(text, records.get(pid))
            if new != text.replace("\r\n", "\n"):
                _write_text(path, new)
                summary["files_updated"] += 1
        except (ValueError, OSError) as exc:
            runlog.event(NAME, "error", pid, "apply_failed", error=repr(exc)[:300])
    return summary
