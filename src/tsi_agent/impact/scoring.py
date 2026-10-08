"""Score d'impact positif (0 a 10), calcule a partir du rapport d'analyse d'impact termine.

L'agent d'analyse ne note pas : un appel LLM separe (sortie structuree) lit le corps du rapport et rend un score et
une justification, ecrits dans le front matter et sous le verdict du rapport.
"""

import json
import math
import re
from pathlib import Path
from typing import Any

from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator, LLMCallError, LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.impact.report import clean_citations, valid_project_id, write_report
from tsi_agent.render import parse_front_matter, render

SCORE_SCHEMA = {
    "type": "object",
    "properties": {"impact_score": {"type": "integer", "minimum": 0, "maximum": 10},
                   "impact_rationale": {"type": "string"}},
    "required": ["impact_score", "impact_rationale"],
}

SCORE_RUBRIC = (
    "Rate the POSITIVE impact of the project, as documented in this impact analysis report, on a 0-10 scale. "
    "0: no evidence of positive impact (outputs not used, reform abandoned, contradicted or reversed). "
    "1-3: weak — outputs delivered but little or no evidence of uptake. "
    "4-6: moderate — the supported reform was adopted or partly implemented, attribution to the project plausible "
    "but partial or early. "
    "7-8: strong — documented results or outcomes plausibly attributable to the project. "
    "9-10: very strong — well-documented, sustained outcomes clearly attributable to the project. "
    "Base the score only on the report; weigh the confidence and attribution caveats it states. "
    "Give a one-sentence rationale citing the report's source numbers [n] where relevant.")

IMPACT_LEVEL_LABELS = {"strong": "Strong", "moderate": "Moderate", "weak": "Weak"}
SCORE_KEYS = ("impact_score", "impact_level", "impact_rationale")
# Cles d'un score precedent (dont l'ancienne echelle -2..+2) retirees avant d'ecrire le nouveau.
_OLD_KEYS = (*SCORE_KEYS, "impact_category")
_SCORE_LINES = ("**Impact:**", "**Positive impact:**")
_SOURCE_REF = re.compile(r"^- \[(\d+)\]", re.MULTILINE)


class ScoringError(ValueError):
    pass


def impact_level(score: int) -> str:
    """7-10 strong, 4-6 moderate, 0-3 weak."""
    return "strong" if score >= 7 else "moderate" if score >= 4 else "weak"


def _body(markdown: str) -> str:
    """Corps du rapport, sans front matter s'il y en a un."""
    if markdown.startswith("---\n"):
        try:
            return parse_front_matter(markdown)[1]
        except ValueError:
            pass
    return markdown


def _score(value: Any) -> int:
    """Entier borne a 0..10 (arrondi) ; ScoringError si la valeur n'est pas un nombre."""
    if isinstance(value, bool) or value is None:
        raise ScoringError(f"invalid impact_score: {value!r}")
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ScoringError(f"invalid impact_score: {value!r}") from exc
    if not math.isfinite(number):
        raise ScoringError(f"invalid impact_score: {value!r}")
    return max(0, min(10, round(number)))


def parse_score(text: str) -> dict[str, Any]:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        raise ScoringError("no JSON object in the scoring reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ScoringError(f"invalid JSON in the scoring reply: {exc}") from exc
    if not isinstance(data, dict):
        raise ScoringError("the scoring reply is not a JSON object")
    return {"impact_score": _score(data.get("impact_score")),
            "impact_rationale": " ".join(str(data.get("impact_rationale") or "").split())}


def score_report(markdown_body: str, generator: Any, *, project_id: str | None = None) -> dict[str, Any]:
    """Score 0..10 et justification d'un rapport ; ScoringError si la reponse est illisible (les erreurs du LLM,
    dont le quota, remontent telles quelles)."""
    reply = generator.run(messages=[ChatMessage.from_system(SCORE_RUBRIC),
                                    ChatMessage.from_user(_body(markdown_body))])
    try:
        result = parse_score(reply["replies"][0].text or "")
    except ScoringError as exc:
        runlog.event("ImpactScoring", "impact_score", project_id, "bad_format", error=str(exc))
        raise
    runlog.event("ImpactScoring", "impact_score", project_id, score=result["impact_score"])
    return result


def default_scorer(settings: Settings) -> Any:
    """Generator a sortie structuree (SCORE_SCHEMA) pour le score d'impact."""
    return LoggedChatGenerator(chat_generator=ClaudeCliChatGenerator(
        model=settings.impact_score_model, timeout_s=settings.impact_llm_timeout_s, json_schema=SCORE_SCHEMA),
        pricing=settings.pricing)


def _source_refs(body: str) -> set[int]:
    """Numeros listes dans la section Sources du rapport (lignes "- [n] ...")."""
    _, sep, sources = body.partition("## 6. Sources")
    return {int(n) for n in _SOURCE_REF.findall(sources)} if sep else set()


def apply_score(markdown: str, score: int, rationale: str) -> str:
    """Rapport complet avec le score : front matter (impact_score, impact_level, impact_rationale apres confidence)
    et ligne "**Positive impact:**" sous le verdict. Remplace un score precedent ; idempotent."""
    front, body = parse_front_matter(markdown)
    score = max(0, min(10, int(score)))
    level = impact_level(score)
    rationale = " ".join(clean_citations(" ".join((rationale or "").split()), _source_refs(body))[0].split())
    values = {"impact_score": score, "impact_level": level, "impact_rationale": rationale}
    kept = {k: v for k, v in (front or {}).items() if k not in _OLD_KEYS}
    if "confidence" in kept:
        new_front: dict[str, Any] = {}
        for key, value in kept.items():
            new_front[key] = value
            if key == "confidence":
                new_front.update(values)
    else:
        new_front = {**kept, **values}

    lines = body.split("\n")
    out: list[str] = []
    skip_blank = False
    for line in lines:
        if line.startswith(_SCORE_LINES):
            skip_blank = True
            continue
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        out.append(line)
    score_line = f"**Positive impact:** {score}/10 ({IMPACT_LEVEL_LABELS[level]})" + (
        f" — {rationale}" if rationale else "")
    at = next((i for i, line in enumerate(out) if line.startswith("**Overall verdict:**")), None)
    if at is None:  # pas de verdict : avant la premiere section
        at = next((i - 1 for i, line in enumerate(out) if line.startswith("## ")), len(out) - 1)
    out[at + 1:at + 1] = ["", score_line]
    return render(new_front, "\n".join(out))


def rescore_reports(settings: Settings, scorer: Any, project_ids: list[str] | None = None) -> dict[str, Any]:
    """Recalcule le score des rapports existants (tous si `project_ids` est vide), dans un run journalise.
    Un echec de score (hors quota) est note dans le resultat et le rapport reste inchange ; le quota interrompt."""
    impact_dir = Path(settings.impact_dir)
    if not project_ids:
        project_ids = sorted(p.stem for p in impact_dir.glob("*.md") if valid_project_id(p.stem))
    results: list[dict[str, Any]] = []
    with runlog.open_run(settings.logs_dir) as run:
        run.extra.update({"mode": "impact-rescore", "reports": len(project_ids)})
        for project_id in project_ids:
            path = impact_dir / f"{project_id}.md"
            if not valid_project_id(project_id) or not path.exists():
                results.append({"project_id": project_id, "error": "no report"})
                runlog.event("ImpactScoring", "alert", project_id, message=f"{project_id} : rapport introuvable")
                continue
            markdown = path.read_text(encoding="utf-8")
            try:
                scored = score_report(_body(markdown), scorer, project_id=project_id)
            except LLMQuotaExceeded:
                raise
            except (ScoringError, LLMCallError) as exc:
                results.append({"project_id": project_id, "error": str(exc)})
                runlog.event("ImpactScoring", "alert", project_id,
                             message=f"{project_id} : score d'impact non calcule ({exc})")
                continue
            write_report(impact_dir, project_id,
                         apply_score(markdown, scored["impact_score"], scored["impact_rationale"]))
            results.append({"project_id": project_id, "impact_score": scored["impact_score"],
                            "impact_level": impact_level(scored["impact_score"])})
    summary = run.final_summary
    return {"run_id": run.run_id, "results": results, "cost_usd": summary["cost_usd"]}
