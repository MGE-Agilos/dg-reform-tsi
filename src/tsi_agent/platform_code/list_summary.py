from collections import Counter
from typing import Optional

from haystack import Document, component

MAX_RESULTS = 10_000  # plafond d'une requete filtree OpenSearch (filter_documents)


def _breakdown(values) -> str:
    counts = Counter(v or "none" for v in values)
    none = counts.pop("none", 0)
    parts = [f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    return ", ".join(parts + ([f"none {none}"] if none else []))


@component
class Code:
    """Compte et liste les projets distincts parmi les morceaux filtres (un projet = un doc_id), comme
    list_projects en local : total, repartition par programme, annee, pays et domaine, puis la liste tronquee."""

    @component.output_types(result=str)
    def run(self, documents: list[Document], countries: Optional[list[str]] = None,
            policy_area: Optional[str] = None, title_contains: Optional[str] = None, limit: int = 20) -> dict:
        wanted = {c.strip().upper() for c in (countries or []) if c and c.strip()}
        projects = {}
        for doc in documents:
            meta = doc.meta or {}
            doc_id = meta.get("doc_id")
            if not doc_id or doc_id in projects:
                continue
            if wanted and not wanted & set(meta.get("countries") or []):
                continue
            if title_contains and title_contains.lower() not in str(meta.get("title") or "").lower():
                continue
            projects[doc_id] = meta
        ordered = sorted(projects.values(), key=lambda m: (-(m.get("year") or 0), str(m.get("title"))))
        by_programme = Counter(m.get("programme") or "?" for m in ordered)
        by_year = Counter(m.get("year") for m in ordered)
        lines = []
        if len(documents) >= MAX_RESULTS:
            lines.append(f"WARNING: the search hit the {MAX_RESULTS}-document limit, totals may be incomplete.")
        lines += [f"Total matching projects: {len(ordered)} ("
                  + ", ".join(f"{k}: {v}" for k, v in sorted(by_programme.items())) + ")",
                  "By year: " + ", ".join(f"{y}: {n}" for y, n in
                                           sorted(by_year.items(), key=lambda kv: -(kv[0] or 0))),
                  "By country: " + _breakdown(c for m in ordered for c in (m.get("countries") or [None]))]
        if not policy_area:
            lines.append("By policy area: " + _breakdown(m.get("policy_area") for m in ordered))
        limit = max(int(limit or 0), 0)
        lines.append(f"Showing {min(limit, len(ordered))} of {len(ordered)}:")
        for m in ordered[:limit]:
            lines.append(f"- {m.get('title')} ({m.get('doc_id')}) — {m.get('programme')} {m.get('year')} — "
                         f"{', '.join(m.get('countries') or [])} — detail page: "
                         f"{'yes' if m.get('detail_page') else 'no'} — {m.get('source_url')}")
        return {"result": "\n".join(lines)}
