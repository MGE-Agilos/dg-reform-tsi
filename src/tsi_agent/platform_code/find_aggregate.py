import re
from typing import Optional

from haystack import Document, component

PROJECT_POOL = 60        # projets gardes depuis la recherche sur les textes projet
DELIVERABLE_POOL = 30    # projets gardes depuis la recherche sur les livrables
MAX_CANDIDATES = 150     # lignes de candidats listees au plus
SNIPPET_CHARS = 140


def _pattern(term: str):
    """Mot ou expression entiere, insensible a la casse, pluriel simple accepte ("reform" trouve "reforms")."""
    return re.compile(rf"(?<!\w){re.escape(term.strip())}(?:s|es)?(?!\w)", re.IGNORECASE)


def _snippet(doc: Optional[Document]) -> str:
    """Debut du texte du morceau, sans sa premiere ligne (titre de contexte "# ...")."""
    if doc is None or not doc.content:
        return ""
    lines = doc.content.strip().splitlines()
    body = " ".join(lines[1:] if lines and lines[0].startswith("#") else lines)
    return " ".join(body.split())[:SNIPPET_CHARS]


@component
class Code:
    """Candidats de find_projects, comme en local : projets dont les textes correspondent le mieux au sujet, projets
    dont les livrables y correspondent, projets dont le titre contient un mot-cle. Pas de seuil de pertinence :
    l'agent juge chaque candidat."""

    @component.output_types(result=str)
    def run(self, topic: str, catalog: list[Document], project_hits: list[Document],
            deliverable_hits: list[Document], keywords: Optional[list[str]] = None,
            countries: Optional[list[str]] = None) -> dict:
        wanted = {c.strip().upper() for c in (countries or []) if c and c.strip()}
        projects, first = {}, {}
        for doc in catalog:
            meta = doc.meta or {}
            pid = meta.get("doc_id")
            if not pid or (wanted and not wanted & set(meta.get("countries") or [])):
                continue
            projects.setdefault(pid, meta)
            if pid not in first or (meta.get("split_id") or 0) < (first[pid].meta.get("split_id") or 0):
                first[pid] = doc

        def best_by_project(hits, deliverables):
            best = {}
            for doc in hits:
                if doc.score is None:
                    continue
                meta = doc.meta or {}
                ids = (meta.get("projects") or []) if deliverables else [meta.get("doc_id")]
                for pid in ids:
                    if pid in projects and (pid not in best or doc.score > best[pid][0]):
                        best[pid] = (doc.score, doc)
            return best

        def top(best, size):
            return sorted(best, key=lambda pid: (-best[pid][0], pid))[:size]

        by_project = best_by_project(project_hits, deliverables=False)
        by_deliverable = best_by_project(deliverable_hits, deliverables=True)
        from_projects = top(by_project, PROJECT_POOL)
        from_deliverables = top(by_deliverable, DELIVERABLE_POOL)

        terms = [k for k in (keywords or []) if k and k.strip()]
        if len(topic.split()) <= 4:
            terms.append(topic)
        patterns = [_pattern(t) for t in terms]
        titled = [pid for pid, meta in projects.items()
                  if any(p.search(str(title)) for p in patterns
                         for title in [meta.get("title") or ""] + list(meta.get("listing_titles") or []))]

        best = dict(by_project)
        for pid, hit in by_deliverable.items():
            if pid not in best or hit[0] > best[pid][0]:
                best[pid] = hit
        via = {pid: [] for pid in [*titled, *from_projects, *from_deliverables]}
        for name, pids in (("title", titled), ("projects", from_projects), ("deliverables", from_deliverables)):
            for pid in pids:
                via[pid].append(name)
        ordered = sorted(via, key=lambda pid: ("title" not in via[pid], -(best[pid][0] if pid in best else -9.0),
                                               str(projects[pid].get("title")), pid))
        lines = [f"Candidate projects for '{topic}': {len(ordered)} ({len(titled)} title matches, "
                 f"{len(from_projects)} from project texts, {len(from_deliverables)} from deliverables). "
                 "These are candidates, not answers: keep only those whose title or snippet is really about the "
                 "topic."]
        if len(ordered) > MAX_CANDIDATES:
            lines.append(f"Showing the first {MAX_CANDIDATES} of {len(ordered)} candidates.")
        for pid in ordered[:MAX_CANDIDATES]:
            meta = projects[pid]
            programme_year = " ".join(str(v) for v in (meta.get("programme"), meta.get("year")) if v)
            parts = [f"- {meta.get('title') or pid} ({pid})", programme_year, ", ".join(meta.get("countries") or []),
                     "via: " + ", ".join(via[pid]), _snippet(best[pid][1] if pid in best else first.get(pid)),
                     meta.get("source_url") or ""]
            lines.append(" — ".join(p for p in parts if p))
        return {"result": "\n".join(lines)}
