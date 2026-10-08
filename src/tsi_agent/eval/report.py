"""Rapport Markdown (en francais) et detail CSV par question du banc de recherche."""

import csv
from pathlib import Path
from typing import Any

from tsi_agent.eval import metrics
from tsi_agent.eval.bench import VARIANTS

LABELS = {"A": "A — Sémantique (actuel)", "B": "B — Sémantique + filtre pays",
          "C": "C — Hybride sémantique + BM25 (RRF)", "D": "D — Sémantique, max 2 morceaux par fichier"}
STRATA = {"project": "Projets", "deliverable": "Livrables (texte intégral)", "other": "Programme / résumés IA"}
LANGS = {"fr": "Français", "en": "Anglais"}
MAX_MISSES = 20
MISS_K = 8


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %"


def points(delta: float) -> str:
    n = round(delta * 100)
    return f"+{n} points" if n > 0 else f"{n} points" if n < 0 else "0 point"


def _ranks(rows: list[dict[str, Any]], variant: str, level: str) -> list[int | None]:
    return [r.get(f"{variant}_{level}") for r in rows]


def _recall_table(rows: list[dict[str, Any]], level: str) -> list[str]:
    head = "| Variante | n | " + " | ".join(f"R@{k}" for k in metrics.KS) + " | MRR |"
    lines = [head, "|" + "---|" * (len(metrics.KS) + 3)]
    for v in VARIANTS:
        s = metrics.summarize(_ranks(rows, v, level))
        mrr = "—" if s["mrr"] is None else f"{s['mrr']:.3f}"
        lines.append(f"| {LABELS[v]} | {s['n']} | " + " | ".join(pct(s[f'r@{k}']) for k in metrics.KS)
                     + f" | {mrr} |")
    return lines


def _breakdown(rows: list[dict[str, Any]], field: str, names: dict[str, str]) -> list[str]:
    lines = [f"| {'Type' if field == 'stratum' else 'Langue'} | n | Morceau R@8 | Fichier R@8 | Projet R@8 | "
             "MRR morceau |", "|---|---|---|---|---|---|"]
    groups = metrics.group_by(rows, field)
    for key in [k for k in names if k in groups] + [k for k in groups if k not in names]:
        group = groups[key]
        chunk = _ranks(group, "A", "chunk")
        project = _ranks([g for g in group if g.get("has_project", True)], "A", "project")
        mrr = metrics.mrr(chunk)
        lines.append(f"| {names.get(key, str(key))} | {len(group)} | {pct(metrics.recall_at(chunk, 8))} | "
                     f"{pct(metrics.recall_at(_ranks(group, 'A', 'file'), 8))} | "
                     f"{pct(metrics.recall_at(project, 8))} | "
                     f"{'—' if mrr is None else f'{mrr:.3f}'} |")
    return lines


def _where(item: dict[str, Any]) -> str:
    parts = [str(item.get("title") or item.get("doc_id") or "?"), str(item.get("section") or "")]
    if item.get("page") is not None:
        parts.append(f"p. {item['page']}")
    return " — ".join(p for p in parts if p)


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _misses(rows: list[dict[str, Any]]) -> list[str]:
    missed = [r for r in rows if r.get("A_chunk") is None or r["A_chunk"] > MISS_K]
    if not missed:
        return [f"Aucune : le bon morceau est dans les {MISS_K} premiers résultats pour toutes les questions."]
    lines = [f"{len(missed)} question(s) sans le bon morceau dans les {MISS_K} premiers résultats (variante A)"
             + (f" ; les {MAX_MISSES} premières :" if len(missed) > MAX_MISSES else " :"), "",
             "| Question | Attendu | Rang morceau / fichier | Trouvé à la place (top 3) |", "|---|---|---|---|"]
    for r in missed[:MAX_MISSES]:
        found = "<br>".join(_cell(_where(t)) for t in r.get("A_top3") or [])
        ranks = f"{r.get('A_chunk') or '—'} / {r.get('A_file') or '—'}"
        lines.append(f"| {_cell(r.get('question') or '')} ({r.get('lang')}) | {_cell(_where(r))} | {ranks} | "
                     f"{found} |")
    return lines


def _chunk_sizes(sizes: list[dict[str, Any]] | None) -> list[str]:
    if not sizes:
        return ["Taille des morceaux non mesurée (option --skip-chunk-sizes)."]
    n = sizes[0]["questions"]
    lines = [f"Sous-corpus projets + programme, index InMemory temporaire, {n} questions de projets. Le morceau "
             "attendu n'existe pas dans les autres découpages : « fichier » = bon fichier dans les k premiers ; "
             "« passage » = un morceau du bon fichier contient au moins la moitié des mots de la phrase-clé du "
             "morceau d'origine. Indicateur, pas une preuve pour les livrables.", "",
             "| Fenêtre (mots) | Chevauchement | Morceaux | Fichier R@8 | Fichier R@24 | Passage R@8 | "
             "Passage R@24 |", "|---|---|---|---|---|---|---|"]
    for s in sizes:
        lines.append(f"| {s['window']} | {s['overlap']} | {s['chunks']} | {pct(s['file@8'])} | "
                     f"{pct(s['file@24'])} | {pct(s['passage@8'])} | {pct(s['passage@24'])} |")
    return lines


def _lecture(rows: list[dict[str, Any]], sizes: list[dict[str, Any]] | None) -> list[str]:
    def r(v: str, level: str, k: int, group: list[dict[str, Any]] = rows) -> float:
        return metrics.recall_at(_ranks(group, v, level), k) or 0.0

    facts = [f"Variante A (actuelle) : à k=8, le bon morceau est trouvé dans {pct(r('A', 'chunk', 8))} des cas "
             f"et le bon fichier dans {pct(r('A', 'file', 8))} ; à k=24 : morceau {pct(r('A', 'chunk', 24))}, "
             f"fichier {pct(r('A', 'file', 24))}. Au premier rang : morceau {pct(r('A', 'chunk', 1))}."]
    for v in VARIANTS[1:]:
        facts.append(f"Variante {LABELS[v]} : {points(r(v, 'chunk', 8) - r('A', 'chunk', 8))} au niveau morceau "
                     f"et {points(r(v, 'file', 8) - r('A', 'file', 8))} au niveau fichier à k=8, par rapport à A.")
    filtered = sum(1 for row in rows if row.get("B_filtered"))
    facts.append(f"Le filtre pays (B) s'applique à {filtered} question(s) sur {len(rows)} (les autres n'ont pas "
                 "de pays connu : B = A).")
    best = max(VARIANTS, key=lambda v: (r(v, "chunk", 8), r(v, "file", 8)))
    facts.append(f"Meilleure variante au niveau morceau à k=8 : {LABELS[best]} ({pct(r(best, 'chunk', 8))}).")
    strata = metrics.group_by(rows, "stratum")
    if len(strata) > 1:
        scored = {k: r("A", "chunk", 8, g) for k, g in strata.items()}
        low, high = min(scored, key=scored.get), max(scored, key=scored.get)
        facts.append(f"Par type (A, morceau, k=8) : le plus bas est {STRATA.get(low, low)} ({pct(scored[low])}), "
                     f"le plus haut {STRATA.get(high, high)} ({pct(scored[high])}).")
    langs = metrics.group_by(rows, "lang")
    if {"fr", "en"} <= set(langs):
        fr, en = r("A", "chunk", 8, langs["fr"]), r("A", "chunk", 8, langs["en"])
        facts.append(f"Par langue (A, morceau, k=8) : français {pct(fr)}, anglais {pct(en)} "
                     f"(écart {points(fr - en)} pour le français).")
    if sizes:
        current = next((s for s in sizes if s["window"] == 250), sizes[0])
        best_size = max(sizes, key=lambda s: (s["passage@8"] or 0, s["file@8"] or 0))
        facts.append("Taille des morceaux (questions de projets, k=8) : " + " ; ".join(
            f"{s['window']} mots → passage {pct(s['passage@8'])}, fichier {pct(s['file@8'])}" for s in sizes)
            + (f". Meilleur passage@8 : {best_size['window']} mots, la taille actuelle." if best_size is current
               else f". Meilleur passage@8 : {best_size['window']} mots "
               f"({points((best_size['passage@8'] or 0) - (current['passage@8'] or 0))} par rapport à "
               f"{current['window']} mots, la taille actuelle)."))
    return [f"- {f}" for f in facts]


def render_report(rows: list[dict[str, Any]], setup: dict[str, Any],
                  chunk_sizes: list[dict[str, Any]] | None = None) -> str:
    strata = metrics.group_by(rows, "stratum")
    langs = metrics.group_by(rows, "lang")
    lines = [f"# Banc de recherche — {setup.get('date')}", "",
             "## Dispositif", "",
             f"- Date : {setup.get('date')}",
             f"- Questions : {len(rows)} (" + ", ".join(f"{STRATA.get(k, k)} {len(v)}" for k, v in strata.items())
             + " ; " + ", ".join(f"{LANGS.get(k, k)} {len(v)}" for k, v in langs.items()) + ")",
             f"- Questions rédigées par : {setup.get('question_model')} (une par morceau tiré au hasard, sans "
             "recopier le texte)",
             f"- Index : {setup.get('index_size')} morceaux ; embedding {setup.get('embedding_model')}",
             f"- Résultats évalués : {setup.get('top_k')} premiers ; candidats pour C et D : "
             f"{setup.get('candidates')} (sémantique) + {setup.get('candidates')} (BM25 pour C)",
             f"- Jeu de questions : {setup.get('questions_file', '—')}", "",
             "Rang = position (à partir de 1) du bon morceau, du bon fichier ou d'un résultat lié au bon projet "
             "dans les résultats ; R@k = part des questions où il est dans les k premiers ; MRR = moyenne de 1/rang "
             "(0 si absent).", "",
             "## Niveau morceau", "", *_recall_table(rows, "chunk"), "",
             "## Niveau fichier", "", *_recall_table(rows, "file"), "",
             "## Niveau projet", "",
             "Un résultat compte s'il s'agit du projet attendu ou d'un livrable de ce projet (questions de "
             "programme, sans projet attendu, exclues).", "",
             *_recall_table([x for x in rows if x.get("has_project", True)], "project"), "",
             "## Détail (variante A, k=8)", "", "### Par type de document", "", *_breakdown(rows, "stratum", STRATA),
             "", "### Par langue", "", *_breakdown(rows, "lang", LANGS), "",
             "## Taille des morceaux", "", *_chunk_sizes(chunk_sizes), "",
             "## Questions ratées (variante A, k=8)", "", *_misses(rows), "",
             "## Lecture", "", *_lecture(rows, chunk_sizes), ""]
    return "\n".join(lines)


CSV_FIELDS = ["id", "lang", "stratum", "doc_type", "ai_generated", "chunk_id", "doc_id", "title", "section", "page",
              "question", *(f"{v}_{level}" for v in VARIANTS for level in ("chunk", "file", "project")),
              "has_project", "B_filtered", "A_top3"]


def write_csv(rows: list[dict[str, Any]], path: str | Path) -> None:
    """Une ligne par question : rangs pour chaque variante (vide = absent des resultats)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({**{k: "" if r.get(k) is None else r.get(k) for k in CSV_FIELDS},
                             "A_top3": " | ".join(_where(t) for t in r.get("A_top3") or [])})
