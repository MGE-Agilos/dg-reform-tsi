"""Rapport Markdown (en francais) et detail CSV du banc d'agregation."""

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from tsi_agent.eval.report import pct

SOURCES = {"metadata": "Métadonnées", "topic": "Thèmes libres"}
KINDS = {"countries": "Pays", "projects": "Projets", "count": "Comptage"}
MAX_LISTED = 15


def _mean(values: list[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return sum(kept) / len(kept) if kept else None


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _items(values: list[str]) -> str:
    values = list(values or [])
    more = f" … (+{len(values) - MAX_LISTED})" if len(values) > MAX_LISTED else ""
    return ", ".join(values[:MAX_LISTED]) + more if values else "—"


def tools_label(tools: list[str]) -> str:
    counts = Counter(tools or [])
    return ", ".join(f"{t} ×{n}" if n > 1 else t for t, n in counts.items()) or "—"


def _groups(rows: list[dict[str, Any]]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    out = []
    for source in SOURCES:
        for kind in KINDS:
            group = [r for r in rows if r.get("source") == source and r.get("kind") == kind]
            if group:
                out.append((source, kind, group))
    return out


def _summary_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = ["| Source | Type | n | Rappel | Précision | F1 | Comptage exact | Écart moyen |",
             "|---|---|---|---|---|---|---|---|"]
    for source, kind, group in _groups(rows):
        if kind == "count":
            answered = [r for r in group if not r.get("error")]
            exact = sum(1 for r in answered if r.get("exact"))
            mae = _mean([r.get("abs_error") for r in answered])
            lines.append(f"| {SOURCES[source]} | {KINDS[kind]} | {len(group)} | — | — | — | {exact}/{len(group)} | "
                         f"{'—' if mae is None else f'{mae:.1f}'} |")
        else:
            lines.append(f"| {SOURCES[source]} | {KINDS[kind]} | {len(group)} | "
                         f"{pct(_mean([r.get('recall') for r in group]))} | "
                         f"{pct(_mean([r.get('precision') for r in group]))} | "
                         f"{pct(_mean([r.get('f1') for r in group]))} | — | — |")
    return lines


def _truth_size(r: dict[str, Any]) -> Any:
    if r["kind"] == "count":
        return r.get("truth_count")
    return len(r.get("truth_countries") if r["kind"] == "countries" else r.get("truth_projects") or [])


def _rows_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = ["| Id | Question | Type | Vérité | Trouvés | Rappel | Précision | Manqués | En trop | Outils |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("error"):
            found, recall, precision, missed, extra = "ERREUR", "—", "—", _cell(r["error"]), "—"
        elif r["kind"] == "count":
            found = "—" if r.get("predicted_count") is None else r["predicted_count"]
            recall, precision = ("exact" if r.get("exact") else f"écart {r.get('abs_error', '—')}"), "—"
            missed = extra = "—"
        else:
            found, recall, precision = len(r.get("found") or []), pct(r.get("recall")), pct(r.get("precision"))
            missed, extra = _items(r.get("missed")), _items(r.get("extra"))
        lines.append(f"| {r['id']} | {_cell(r['question'])} ({r.get('lang')}) | {KINDS.get(r['kind'], r['kind'])} | "
                     f"{_truth_size(r)} | {found} | {recall} | {precision} | {missed} | {extra} | "
                     f"{tools_label(r.get('tools'))} |")
    return lines


def _topics_table(topics: list[dict[str, Any]]) -> list[str]:
    if not topics:
        return ["Aucun thème libre dans ce run."]
    lines = ["| Thème | Candidats jugés | Projets pertinents | Pays | Non jugés |", "|---|---|---|---|---|"]
    for t in topics:
        lines.append(f"| {t['id']} | {t['candidates']} | {len(t['relevant'])} | {len(t.get('countries') or [])} | "
                     f"{t.get('unjudged', 0)} |")
    return lines


def _lecture(rows: list[dict[str, Any]]) -> list[str]:
    facts = []
    for source, label in SOURCES.items():
        group = [r for r in rows if r.get("source") == source and r["kind"] != "count"]
        if group:
            facts.append(f"{label} (questions pays et projets, {len(group)}) : rappel moyen "
                         f"{pct(_mean([r.get('recall') for r in group]))}, précision moyenne "
                         f"{pct(_mean([r.get('precision') for r in group]))}, F1 moyen "
                         f"{pct(_mean([r.get('f1') for r in group]))}.")
    counts = [r for r in rows if r["kind"] == "count"]
    if counts:
        mae = _mean([r.get("abs_error") for r in counts if not r.get("error")])
        facts.append(f"Comptages : {sum(1 for r in counts if r.get('exact'))}/{len(counts)} exacts"
                     + ("" if mae is None else f", écart absolu moyen {mae:.1f}") + ".")
    for source, label in SOURCES.items():
        group = [r for r in rows if r.get("source") == source and not r.get("error")]
        if group:
            used = sum(1 for r in group if "list_projects" in (r.get("tools") or []))
            searched = sum(1 for r in group if {"search_projects", "search_deliverables"} & set(r.get("tools") or []))
            facts.append(f"{label} : list_projects appelé dans {used}/{len(group)} questions, une recherche "
                         f"sémantique dans {searched}/{len(group)}.")
    scored = [r for r in rows if r.get("recall") is not None]
    if scored:
        low = min(scored, key=lambda r: (r["recall"], r["id"]))
        facts.append(f"Rappel le plus bas : {low['id']} « {_cell(low['question'])} » ({pct(low['recall'])}, "
                     f"{len(low.get('missed') or [])} manqué(s) sur {_truth_size(low)}).")
        full = sum(1 for r in scored if r["recall"] == 1.0)
        facts.append(f"Rappel complet (100 %) pour {full}/{len(scored)} questions pays et projets.")
    errors = [r for r in rows if r.get("error")]
    if errors:
        facts.append(f"{len(errors)} question(s) en erreur : " + ", ".join(r["id"] for r in errors) + ".")
    cost = sum(r.get("cost_usd") or 0.0 for r in rows)
    duration = sum(r.get("duration_s") or 0.0 for r in rows)
    facts.append(f"Chatbot : {cost:.2f} $ et {duration / 60:.1f} min au total pour {len(rows)} questions.")
    return [f"- {f}" for f in facts]


def render_report(rows: list[dict[str, Any]], setup: dict[str, Any]) -> str:
    by_source = Counter(SOURCES.get(r.get("source"), r.get("source")) for r in rows)
    lines = [f"# Banc d'agrégation — {setup.get('date')}", "",
             "## Dispositif", "",
             f"- Date : {setup.get('date')}",
             f"- Questions : {len(rows)} (" + ", ".join(f"{k} {v}" for k, v in by_source.items()) + "), posées une "
             "à une au chatbot, sans historique",
             f"- Modèle du chatbot : {setup.get('chat_model')} ; juge des thèmes libres : {setup.get('judge_model')}",
             f"- Vérité des thèmes libres : {setup.get('truth_file') or '—'}", "",
             "Vérité des métadonnées : catalogue des projets (domaine et pays du front matter), exacte. Vérité des "
             "thèmes libres : candidats (mots-clés du titre + projets des 300 premiers morceaux sémantiques), puis "
             "jugement projet par projet par le juge LLM. Pays trouvés = pays nommés (anglais ou français) ou codes "
             "ISO dans le texte de la réponse ; projets trouvés = sources citées + titres ou identifiants de projets "
             "recopiés dans la réponse. Rappel = part de la vérité trouvée ; précision = part des éléments trouvés qui "
             "sont dans la vérité.", "",
             "## Résultats par type de question", "", *_summary_table(rows), "",
             "## Vérité des thèmes libres", "", *_topics_table(setup.get("topics") or []), "",
             "## Détail par question", "", *_rows_table(rows), "",
             "## Lecture", "", *_lecture(rows), ""]
    return "\n".join(lines)


CSV_FIELDS = ["id", "source", "kind", "lang", "topic", "question", "truth_size", "found_size", "recall", "precision",
              "f1", "truth_count", "predicted_count", "abs_error", "exact", "missed", "extra", "tools", "duration_s",
              "cost_usd", "error", "answer"]


def write_csv(rows: list[dict[str, Any]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            out = {k: "" if r.get(k) is None else r.get(k) for k in CSV_FIELDS}
            out.update(truth_size=_truth_size(r), found_size="" if r["kind"] == "count" else len(r.get("found") or []),
                       missed=" ".join(r.get("missed") or []), extra=" ".join(r.get("extra") or []),
                       tools=tools_label(r.get("tools")))
            writer.writerow(out)
