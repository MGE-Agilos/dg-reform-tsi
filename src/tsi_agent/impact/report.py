"""Rapport d'analyse d'impact : reponse JSON de l'agent -> Markdown (front matter + sections fixes).

La section Sources est construite par le code a partir des sources reellement renvoyees par les outils ; toute
citation [n] inconnue est retiree (garde-fou contre les liens inventes).
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

from tsi_agent.render import parse_front_matter, render

VERDICTS = ("achieved", "partly achieved", "not achieved", "insufficient evidence")
CONFIDENCES = ("high", "medium", "low")
# Groupe de citations : [3], [2, 7], [2; 3], [1-2], [3–5] (tiret ou demi-cadratin pour les plages).
_CITE = re.compile(r"\[(\d+(?:\s*[,;\-\u2013]\s*\d+)*)\]")
_RANGE = re.compile(r"(\d+)\s*[\-\u2013]\s*(\d+)|(\d+)")
MAX_CITATION = 999  # au-dela : une annee entre crochets ([2023]), pas une citation
MAX_RANGE = 20  # plage plus longue : laissee telle quelle
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,200}$")


_TEXT = {"type": "string"}
# Schema de la reponse finale : sert au reformatage (sortie structuree du CLI) quand le JSON de l'agent est invalide.
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "project_overview": _TEXT, "positive_evidence": _TEXT, "negative_evidence": _TEXT,
        "impact_by_level": {"type": "object", "properties": {"country": _TEXT, "europe": _TEXT, "world": _TEXT},
                            "required": ["country", "europe", "world"]},
        "objectives": {"type": "array", "items": {
            "type": "object",
            "properties": {"objective": _TEXT, "verdict": {"type": "string", "enum": list(VERDICTS)},
                           "rationale": _TEXT},
            "required": ["objective", "verdict", "rationale"]}},
        "overall_verdict": _TEXT,
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
    },
    "required": ["project_overview", "positive_evidence", "negative_evidence", "impact_by_level", "objectives",
                 "overall_verdict", "confidence"],
}


class ReportFormatError(ValueError):
    pass


def valid_project_id(project_id: str) -> bool:
    return bool(_PROJECT_ID.fullmatch(project_id or ""))


def parse_report(text: str) -> dict[str, Any]:
    """Premier objet JSON de la reponse (avec ou sans bloc ```json) ; champs manquants completes."""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        raise ReportFormatError("no JSON object in the final answer")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ReportFormatError(f"invalid JSON in the final answer: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportFormatError("final answer is not a JSON object")
    levels = data.get("impact_by_level") if isinstance(data.get("impact_by_level"), dict) else {}
    objectives = []
    for item in data.get("objectives") or []:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        objectives.append({"objective": str(item.get("objective") or "").strip(),
                           "verdict": verdict if verdict in VERDICTS else "insufficient evidence",
                           "rationale": str(item.get("rationale") or "").strip()})
    confidence = str(data.get("confidence") or "").strip().lower()
    report = {
        "project_overview": str(data.get("project_overview") or "").strip(),
        "positive_evidence": str(data.get("positive_evidence") or "").strip(),
        "negative_evidence": str(data.get("negative_evidence") or "").strip(),
        "impact_by_level": {k: str(levels.get(k) or "").strip() for k in ("country", "europe", "world")},
        "objectives": objectives,
        "overall_verdict": str(data.get("overall_verdict") or "").strip(),
        "confidence": confidence if confidence in CONFIDENCES else "low",
    }
    texts = [report["overall_verdict"], report["project_overview"], report["positive_evidence"],
             report["negative_evidence"], *report["impact_by_level"].values()]
    if not any(texts):  # reponse vide : ne doit jamais ecraser un rapport existant
        raise ReportFormatError("the final answer has no verdict and no content")
    return report


def _refs(group: str) -> list[int] | None:
    """Numeros d'un groupe de citations, plages developpees ; None si ce n'est pas une citation (annee, plage
    trop longue ou inversee)."""
    refs: list[int] = []
    for start, end, single in _RANGE.findall(group):
        if single:
            refs.append(int(single))
            continue
        a, b = int(start), int(end)
        if b < a or b - a + 1 > MAX_RANGE:
            return None
        refs.extend(range(a, b + 1))
    if any(n > MAX_CITATION for n in refs):
        return None
    return refs


def clean_citations(text: str, valid: set[int]) -> tuple[str, list[int]]:
    """Retire les numeros [n] inconnus ; "[2, 7]" devient "[2]" (ou rien), "[1-3]" devient "[1][2][3]".
    Renvoie (texte, numeros retires)."""
    removed: set[int] = set()

    def repl(m: re.Match) -> str:
        refs = _refs(m.group(1))
        if refs is None:
            return m.group(0)
        removed.update(n for n in refs if n not in valid)
        return "".join(f"[{n}]" for n in refs if n in valid)

    return _CITE.sub(repl, text or ""), sorted(removed)


def _cell(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def _cited(texts: list[str]) -> set[int]:
    return {n for t in texts for group in _CITE.findall(t) for n in (_refs(group) or [])}


def _without_headings(text: str) -> str:
    """Titres Markdown ajoutes par le modele dans une section ("## Positive Evidence") : en gras, pour ne pas casser
    la structure fixe du rapport."""
    return re.sub(r"^#{1,6}\s+(.+?)\s*#*\s*$", r"****", text or "", flags=re.MULTILINE)


def render_report(meta: dict[str, Any], report: dict[str, Any], sources: list[dict[str, Any]],
                  run: dict[str, Any]) -> str:
    valid = {int(s["ref"]) for s in sources if s.get("ref") is not None}
    removed: set[int] = set()

    def clean(text: str) -> str:
        cleaned, gone = clean_citations(_without_headings(text), valid)
        removed.update(gone)
        return cleaned

    overview = clean(report["project_overview"])
    positive = clean(report["positive_evidence"])
    negative = clean(report["negative_evidence"])
    levels = {k: clean(v) for k, v in report["impact_by_level"].items()}
    objectives = [{**o, "rationale": clean(o["rationale"]),
                   "verdict": o["verdict"] if o["verdict"] in VERDICTS else "insufficient evidence"}
                  for o in report["objectives"]]
    verdict = clean(report["overall_verdict"])
    cited = _cited([overview, positive, negative, verdict, *levels.values(), *(o["rationale"] for o in objectives)])
    used = sorted((s for s in sources if s.get("ref") in cited), key=lambda s: s["ref"])

    scope = " — ".join(p for p in (" ".join(str(v) for v in (meta.get("programme"), meta.get("year")) if v),
                                   ", ".join(meta.get("countries") or []), meta.get("policy_area")) if p)
    table = ["| Objective | Verdict | Rationale |", "|---|---|---|"]
    table += [f"| {_cell(o['objective'])} | {o['verdict']} | {_cell(o['rationale'])} |" for o in objectives]
    lines = []
    for s in used:
        parts = [s.get("title") or s.get("url"), s.get("section"), s.get("url")]
        lines.append(f"- [{s['ref']}] " + " — ".join(str(p) for p in parts if p))
    body = "\n\n".join([
        f"# Impact analysis — {meta.get('title') or meta.get('project_id')}",
        f"{scope}  \nGenerated {run['generated']} · {run['model']} · run {run['run_id']}",
        f"**Overall verdict:** {verdict} (confidence: {report['confidence']})",
        f"## 1. Project overview\n\n{overview}",
        f"## 2. Evidence of positive impact\n\n{positive}",
        f"## 3. Evidence of negative impact or limitations\n\n{negative}",
        "## 4. Impact by level\n\n" + "\n\n".join(f"### {label}\n\n{levels[key] or 'No evidence found.'}"
                                                  for key, label in (("country", "Country"), ("europe", "Europe"),
                                                                     ("world", "World"))),
        "## 5. Assessment against objectives\n\n" + "\n".join(table),
        "## 6. Sources\n\n" + ("\n".join(lines) if lines else "No sources cited."),
    ])
    front = {"project_id": meta.get("project_id"), "title": meta.get("title"), "countries": meta.get("countries"),
             "year": meta.get("year"), "generated": run["generated"], "run_id": run["run_id"],
             "model": run["model"], "cost_usd": run.get("cost_usd"), "duration_s": run.get("duration_s"),
             "overall_verdict": verdict, "confidence": report["confidence"], "sources": len(used),
             "removed_citations": sorted(removed)}
    return render(front, body)


def write_report(impact_dir: str | Path, project_id: str, markdown: str) -> Path:
    if not valid_project_id(project_id):
        raise ValueError(f"invalid project id: {project_id!r}")
    path = Path(impact_dir) / f"{project_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def read_report(impact_dir: str | Path, project_id: str) -> tuple[dict[str, Any], str] | None:
    if not valid_project_id(project_id):
        return None
    path = Path(impact_dir) / f"{project_id}.md"
    if not path.exists():
        return None
    return parse_front_matter(path.read_text(encoding="utf-8"))


def list_reports(impact_dir: str | Path) -> list[dict[str, Any]]:
    """Front matter des rapports existants, le plus recent d'abord."""
    out = []
    for path in Path(impact_dir).glob("*.md"):
        try:
            front = parse_front_matter(path.read_text(encoding="utf-8"))[0]
        except (ValueError, OSError, yaml.YAMLError):  # fichier illisible : ignore, la liste reste servie
            continue
        if isinstance(front, dict) and front:
            out.append(front)
    return sorted(out, key=lambda f: (str(f.get("generated") or ""), str(f.get("run_id") or "")), reverse=True)
