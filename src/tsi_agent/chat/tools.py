"""Outils de l'agent chatbot : recherche semantique (projets, texte integral des livrables), recherche des
projets d'un theme libre (find_projects), liste/comptage de projets, lecture d'un projet.

Composants Haystack exposes via ComponentTool. Les filtres annee/programme/domaine/detail passent par les
filtres Haystack standards (meme comportement InMemory et OpenSearch) ; le filtre pays est applique ici en
Python, un filtre sur un champ liste n'ayant pas la meme semantique selon le store.
"""

import re
import time
from collections import Counter
from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict, import_class_by_name
from haystack.utils import deserialize_component_inplace

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, slug_from_iso

SEARCH_CANDIDATES_WITH_FILTER = 100
SNIPPET_CHARS = 1200
READER_MAX_CHARS = 8000
LISTER_DEFAULT_LIMIT = 20
# find_projects : valeurs par defaut, remplacees par les reglages finder_* dans build_chat_pipeline.
FINDER_PROJECT_POOL = 60
FINDER_PROJECT_CHUNKS = 400
FINDER_DELIVERABLE_POOL = 30
FINDER_DELIVERABLE_CHUNKS = 300
FINDER_MAX_CANDIDATES = 150
FINDER_SNIPPET_CHARS = 140


def build_filters(year_from: int | None = None, year_to: int | None = None, programme: str | None = None,
                  policy_area: str | None = None, detail_only: bool = False,
                  doc_type: str | list[str] | None = "project", countries: list[str] | None = None,
                  project_id: str | None = None) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    if doc_type:
        if isinstance(doc_type, str):
            conditions.append({"field": "meta.doc_type", "operator": "==", "value": doc_type})
        else:
            conditions.append({"field": "meta.doc_type", "operator": "in", "value": list(doc_type)})
    if year_from is not None:
        conditions.append({"field": "meta.year", "operator": ">=", "value": int(year_from)})
    if year_to is not None:
        conditions.append({"field": "meta.year", "operator": "<=", "value": int(year_to)})
    if programme:
        conditions.append({"field": "meta.programme", "operator": "==", "value": programme.upper()})
    if policy_area:
        conditions.append({"field": "meta.policy_area", "operator": "==", "value": policy_area})
    if detail_only:
        conditions.append({"field": "meta.detail_page", "operator": "==", "value": True})
    # Champs liste : `in` avec une liste correspond si l'un des elements du champ est dans la liste (Qdrant, OpenSearch).
    if countries:
        conditions.append({"field": "meta.countries", "operator": "in",
                           "value": [c.strip().upper() for c in countries]})
    if project_id:
        conditions.append({"field": "meta.projects", "operator": "in", "value": [project_id]})
    return {"operator": "AND", "conditions": conditions} if conditions else None


def _in_countries(doc: Document, countries: list[str] | None) -> bool:
    if not countries:
        return True
    wanted = {c.strip().upper() for c in countries}
    doc_id = doc.meta.get("doc_id") or ""
    if doc.meta.get("doc_type") == "programme" and doc_id.startswith("country-"):
        # Fiche pays : pas de champ "countries" en meta, le pays vient de son doc_id ("country-<slug>").
        return any(doc_id == f"country-{slug_from_iso(iso)}" for iso in wanted if iso in COUNTRIES)
    return bool(wanted & set(doc.meta.get("countries") or []))


def _label(meta: dict[str, Any]) -> str:
    programme = " ".join(str(v) for v in (meta.get("programme"), meta.get("year")) if v)
    countries = ", ".join(meta.get("countries") or [])
    return " — ".join(p for p in (meta.get("title") or meta.get("doc_id"), programme, countries) if p)


def _source(ref: int, doc: Document, section: str | None = None, ai_generated: bool = False) -> dict[str, Any]:
    return {"ref": ref, "doc_id": doc.meta.get("doc_id"), "title": doc.meta.get("title") or doc.meta.get("doc_id"),
            "url": doc.meta.get("source_url"), "section": section, "ai_generated": ai_generated}


def _breakdown(values: Any) -> str:
    """Comptage "cle n, ..." par nombre decroissant puis cle ; valeur absente comptee sous "none", en dernier."""
    counts = Counter(v or "none" for v in values)
    none = counts.pop("none", 0)
    parts = [f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    return ", ".join(parts + ([f"none {none}"] if none else []))


def _deserialize_object(data: dict[str, Any]) -> Any:
    return import_class_by_name(data["type"]).from_dict(data)


@component
class ProjectSearch:
    def __init__(self, text_embedder: Any, retriever: Any, top_k: int = 8):
        """:param text_embedder: embedder de requete ; :param retriever: retriever par embedding du document store."""
        self.text_embedder = text_embedder
        self.retriever = retriever
        self.top_k = top_k

    def warm_up(self) -> None:
        if hasattr(self.text_embedder, "warm_up"):
            self.text_embedder.warm_up()

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, top_k=self.top_k,
                               text_embedder=component_to_dict(obj=self.text_embedder, name="text_embedder"),
                               retriever=component_to_dict(obj=self.retriever, name="retriever"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectSearch":
        deserialize_component_inplace(data["init_parameters"], key="text_embedder")
        deserialize_component_inplace(data["init_parameters"], key="retriever")
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict])
    def run(self, query: str, countries: list[str] | None = None, year_from: int | None = None,
            year_to: int | None = None, programme: str | None = None, policy_area: str | None = None,
            detail_only: bool = False, include_programme: bool = False,
            known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Semantic search in the TSI/SRSP project texts. Returns numbered passages [n] to cite.

        :param query: What to look for, in English (e.g. "reform of the healthcare system").
        :param countries: ISO country codes to keep (e.g. ["HR", "SI"]; Greece is "GR").
        :param year_from: Keep projects from this year on (e.g. 2022).
        :param year_to: Keep projects up to this year.
        :param programme: "TSI" (2021 onwards), "SRSP" (2017-2020) or "SRSS" (pre-2017 technical assistance to Greece).
        :param policy_area: Policy area slug (e.g. "green-transition").
        :param detail_only: Keep only projects that have a detail page (richer descriptions).
        :param include_programme: Also search country factsheets and policy area pages (programme figures).
        """
        t0 = time.monotonic()
        embedding = self.text_embedder.run(text=query)["embedding"]
        # Les fiches pays (include_programme) n'ont pas de champ countries : pays filtre en Python dans ce cas.
        filters = build_filters(year_from, year_to, programme, policy_area, detail_only,
                                doc_type=["project", "programme"] if include_programme else "project",
                                countries=None if include_programme else countries)
        candidates = SEARCH_CANDIDATES_WITH_FILTER if (countries and include_programme) else self.top_k * 3
        docs = self.retriever.run(query_embedding=embedding, filters=filters, top_k=candidates)["documents"]
        docs = [d for d in docs if _in_countries(d, countries)][: self.top_k]
        start = len(known_sources or [])
        sources: list[dict[str, Any]] = []
        blocks: list[str] = []
        for i, doc in enumerate(docs, start=start + 1):
            ai = bool(doc.meta.get("ai_generated"))
            section = doc.meta.get("section")
            score = round(doc.score, 3) if doc.score is not None else None
            source = _source(i, doc, section, ai)
            source["score"] = score
            sources.append(source)
            flag = " [AI-generated summary]" if ai else ""
            score_txt = f" — score {score}" if score is not None else ""
            blocks.append(f"[{i}] {_label(doc.meta)} — section: {section}{flag}{score_txt}\n"
                          f"URL: {doc.meta.get('source_url')}\n{(doc.content or '')[:SNIPPET_CHARS]}")
        result = "\n\n".join(blocks) if blocks else "No matching passages found."
        runlog.event("ProjectSearch", "tool_call", "search_projects", query=query, countries=countries,
                     filters=filters, results=len(docs), duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": result, "sources": sources}


@component
class DeliverableSearch(ProjectSearch):
    @component.output_types(result=str, sources=list[dict])
    def run(self, query: str, countries: list[str] | None = None, year_from: int | None = None,
            year_to: int | None = None, programme: str | None = None, policy_area: str | None = None,
            publication_id: str | None = None, project_id: str | None = None,
            known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Semantic search in the full text of the projects' deliverables (reports, presentations). Returns numbered
        passages [n] with the document and page, to cite.

        :param query: What to look for, in English (e.g. "survey sample size and response rate").
        :param countries: ISO country codes of the related projects (e.g. ["HR", "SI"]; Greece is "GR").
        :param year_from: Keep deliverables of projects from this year on.
        :param year_to: Keep deliverables of projects up to this year.
        :param programme: "TSI" (2021 onwards), "SRSP" (2017-2020) or "SRSS" (pre-2017 technical assistance to Greece).
        :param policy_area: Policy area slug (e.g. "green-transition").
        :param publication_id: Keep only this publication (id as returned in earlier results).
        :param project_id: Keep only deliverables linked to this project id.
        """
        t0 = time.monotonic()
        embedding = self.text_embedder.run(text=query)["embedding"]
        filters = build_filters(year_from, year_to, programme, policy_area, doc_type="deliverable",
                                countries=countries, project_id=project_id)
        if publication_id:
            filters["conditions"].append({"field": "meta.doc_id", "operator": "==", "value": publication_id})
        candidates = self.top_k * 3
        docs = self.retriever.run(query_embedding=embedding, filters=filters, top_k=candidates)["documents"]
        docs = [d for d in docs if _in_countries(d, countries)
                and (not project_id or project_id in (d.meta.get("projects") or []))][: self.top_k]
        start = len(known_sources or [])
        sources: list[dict[str, Any]] = []
        blocks: list[str] = []
        for i, doc in enumerate(docs, start=start + 1):
            meta = doc.meta
            where = f"{meta.get('section')}, p. {meta['page']}" if meta.get("page") is not None else str(meta.get("section"))
            score = round(doc.score, 3) if doc.score is not None else None
            sources.append({"ref": i, "doc_id": meta.get("doc_id"), "title": meta.get("title") or meta.get("doc_id"),
                            "url": meta.get("source_url"), "section": where, "ai_generated": False, "score": score})
            programme_year = " ".join(str(v) for v in (meta.get("programme"), meta.get("year")) if v)
            head = " — ".join(p for p in (meta.get("title") or meta.get("doc_id"), where, programme_year,
                                          ", ".join(meta.get("countries") or [])) if p)
            blocks.append(f"[{i}] {head}\nURL: {meta.get('source_url')}\n{(doc.content or '')[:SNIPPET_CHARS]}")
        result = "\n\n".join(blocks) if blocks else "No matching passages found."
        runlog.event("DeliverableSearch", "tool_call", "search_deliverables", query=query, countries=countries,
                     publication_id=publication_id, project_id=project_id, filters=filters, results=len(docs),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": result, "sources": sources}


def _term_pattern(term: str) -> "re.Pattern[str]":
    """Mot ou expression entiere, insensible a la casse, pluriel simple accepte ("reform" trouve "reforms")."""
    return re.compile(rf"(?<!\w){re.escape(term.strip())}(?:s|es)?(?!\w)", re.IGNORECASE)


def _snippet(text: str | None) -> str:
    return " ".join((text or "").split())[:FINDER_SNIPPET_CHARS]


@component
class ProjectFinder:
    def __init__(self, text_embedder: Any, retriever: Any, document_store: Any,
                 project_pool: int = FINDER_PROJECT_POOL, project_chunks: int = FINDER_PROJECT_CHUNKS,
                 deliverable_pool: int = FINDER_DELIVERABLE_POOL,
                 deliverable_chunks: int = FINDER_DELIVERABLE_CHUNKS, max_candidates: int = FINDER_MAX_CANDIDATES):
        """:param project_pool: projets gardes depuis la recherche sur les textes projet ;
        :param project_chunks: morceaux projet lus avant regroupement ; :param deliverable_pool: projets gardes
        depuis la recherche sur les livrables ; :param deliverable_chunks: morceaux livrable lus ;
        :param max_candidates: lignes de candidats listees au plus."""
        self.text_embedder = text_embedder
        self.retriever = retriever
        self.document_store = document_store
        self.project_pool = project_pool
        self.project_chunks = project_chunks
        self.deliverable_pool = deliverable_pool
        self.deliverable_chunks = deliverable_chunks
        self.max_candidates = max_candidates

    def warm_up(self) -> None:
        if hasattr(self.text_embedder, "warm_up"):
            self.text_embedder.warm_up()

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, project_pool=self.project_pool, project_chunks=self.project_chunks,
                               deliverable_pool=self.deliverable_pool, deliverable_chunks=self.deliverable_chunks,
                               max_candidates=self.max_candidates, document_store=self.document_store.to_dict(),
                               text_embedder=component_to_dict(obj=self.text_embedder, name="text_embedder"),
                               retriever=component_to_dict(obj=self.retriever, name="retriever"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectFinder":
        deserialize_component_inplace(data["init_parameters"], key="text_embedder")
        deserialize_component_inplace(data["init_parameters"], key="retriever")
        data["init_parameters"]["document_store"] = _deserialize_object(data["init_parameters"]["document_store"])
        return default_from_dict(cls, data)

    def _best_by_project(self, embedding: list[float], filters: dict[str, Any] | None, top_k: int,
                         countries: list[str] | None, projects: dict[str, dict[str, Any]]
                         ) -> dict[str, tuple[float, Document]]:
        """Meilleur morceau par projet (doc_id d'un morceau projet, meta.projects d'un morceau livrable)."""
        best: dict[str, tuple[float, Document]] = {}
        for doc in self.retriever.run(query_embedding=embedding, filters=filters, top_k=top_k)["documents"]:
            if not _in_countries(doc, countries) or doc.score is None:
                continue
            ids = [doc.meta.get("doc_id")] if doc.meta.get("doc_type") == "project" else doc.meta.get("projects") or []
            for pid in ids:
                # Un livrable peut citer un projet hors filtres : seuls les projets retenus comptent.
                if pid in projects and (pid not in best or doc.score > best[pid][0]):
                    best[pid] = (doc.score, doc)
        return best

    @staticmethod
    def _top(best: dict[str, tuple[float, Document]], size: int) -> list[str]:
        return sorted(best, key=lambda pid: (-best[pid][0], pid))[: max(size, 0)]

    @component.output_types(result=str, sources=list[dict])
    def run(self, topic: str, keywords: list[str] | None = None, countries: list[str] | None = None,
            year_from: int | None = None, year_to: int | None = None, programme: str | None = None,
            policy_area: str | None = None, known_sources: list[dict] | None = None) -> dict[str, Any]:
        """List CANDIDATE projects for a free topic, over the whole corpus, for you to judge. The pool combines
        the projects whose texts best match the topic, the projects whose deliverables best match it, and the
        projects whose title contains one of the keywords. There is no relevance threshold: many candidates are
        off-topic. Keep only those whose title or snippet is really about the topic, then count them yourself.
        Returns numbered candidates [n] to cite.

        :param topic: English description of the topic (e.g. "anti-money laundering and counter-terrorist
            financing").
        :param keywords: 3-8 English keywords, synonyms or acronyms searched in project titles (e.g.
            ["money laundering", "AML", "CFT", "financial intelligence", "FIU"]).
        :param countries: ISO country codes (e.g. ["HR", "SI"]; Greece is "GR"); a project matches if it involves
            any of them.
        :param year_from: Keep projects from this year on.
        :param year_to: Keep projects up to this year.
        :param programme: "TSI" (2021 onwards), "SRSP" (2017-2020) or "SRSS" (pre-2017 technical assistance to Greece).
        :param policy_area: Policy area slug (e.g. "green-transition").
        """
        t0 = time.monotonic()
        # Metadonnees de tous les projets correspondant aux filtres (un par doc_id) et premier morceau de chacun
        # (extrait des candidats trouves par le titre seul).
        projects: dict[str, dict[str, Any]] = {}
        first: dict[str, Document] = {}
        for doc in self.document_store.filter_documents(
                filters=build_filters(year_from, year_to, programme, policy_area, countries=countries)):
            doc_id = doc.meta.get("doc_id")
            if not doc_id or not _in_countries(doc, countries):
                continue
            projects.setdefault(doc_id, doc.meta)
            if doc_id not in first or (doc.meta.get("chunk_index") or 0) < (first[doc_id].meta.get("chunk_index") or 0):
                first[doc_id] = doc

        # Deux recherches separees : les gros rapports ne prennent pas la place des textes projet.
        embedding = self.text_embedder.run(text=topic)["embedding"]
        by_project = self._best_by_project(
            embedding, build_filters(year_from, year_to, programme, policy_area, doc_type="project",
                                     countries=countries), self.project_chunks, countries, projects)
        by_deliverable = self._best_by_project(
            embedding, build_filters(year_from, year_to, programme, policy_area, doc_type="deliverable",
                                     countries=countries), self.deliverable_chunks, countries, projects)
        from_projects = self._top(by_project, self.project_pool)
        from_deliverables = self._top(by_deliverable, self.deliverable_pool)

        # Mots-cles : titres et titres de listing de tous les projets.
        terms = [k for k in (keywords or []) if k and k.strip()]
        if len(topic.split()) <= 4:
            terms.append(topic)
        patterns = [_term_pattern(t) for t in terms]
        titled = [pid for pid, meta in projects.items()
                  if any(p.search(str(title)) for p in patterns
                         for title in [meta.get("title") or ""] + list(meta.get("listing_titles") or []))]

        # Meilleur morceau toutes recherches confondues (score de tri et extrait), meme hors des pools.
        best = dict(by_project)
        for pid, hit in by_deliverable.items():
            if pid not in best or hit[0] > best[pid][0]:
                best[pid] = hit
        via: dict[str, list[str]] = {pid: [] for pid in [*titled, *from_projects, *from_deliverables]}
        for name, pids in (("title", titled), ("projects", from_projects), ("deliverables", from_deliverables)):
            for pid in pids:
                via[pid].append(name)
        ordered = sorted(via, key=lambda pid: ("title" not in via[pid], -(best[pid][0] if pid in best else -9.0),
                                               str(projects[pid].get("title")), pid))
        lines = [f"Candidate projects for '{topic}': {len(ordered)} ({len(titled)} title matches, "
                 f"{len(from_projects)} from project texts, {len(from_deliverables)} from deliverables). "
                 "These are candidates, not answers: keep only those whose title or snippet is really about the "
                 "topic."]
        if len(ordered) > self.max_candidates:
            lines.append(f"Showing the first {self.max_candidates} of {len(ordered)} candidates.")
        start = len(known_sources or [])
        sources: list[dict[str, Any]] = []
        for i, pid in enumerate(ordered[: self.max_candidates], start=start + 1):
            meta = projects[pid]
            score = round(best[pid][0], 3) if pid in best else None
            snippet_doc = best[pid][1] if pid in best else first.get(pid)
            programme_year = " ".join(str(v) for v in (meta.get("programme"), meta.get("year")) if v)
            parts = [f"[{i}] {meta.get('title') or pid} ({pid})", programme_year,
                     ", ".join(meta.get("countries") or []), "via: " + ", ".join(via[pid]),
                     _snippet(snippet_doc.content if snippet_doc else None), meta.get("source_url") or ""]
            lines.append(" — ".join(p for p in parts if p))
            sources.append({"ref": i, "doc_id": pid, "title": meta.get("title") or pid, "url": meta.get("source_url"),
                            "section": None, "ai_generated": False, "score": score})
        runlog.event("ProjectFinder", "tool_call", "find_projects", topic=topic, keywords=keywords,
                     candidates=len(ordered), title_matches=len(titled), from_projects=len(from_projects),
                     from_deliverables=len(from_deliverables), duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": "\n".join(lines), "sources": sources}


@component
class ProjectLister:
    def __init__(self, document_store: Any):
        self.document_store = document_store

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, document_store=self.document_store.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectLister":
        data["init_parameters"]["document_store"] = _deserialize_object(data["init_parameters"]["document_store"])
        return default_from_dict(cls, data)

    @component.output_types(result=str)
    def run(self, countries: list[str] | None = None, year_from: int | None = None, year_to: int | None = None,
            programme: str | None = None, policy_area: str | None = None, detail_only: bool = False,
            title_contains: str | None = None, limit: int = LISTER_DEFAULT_LIMIT) -> dict[str, Any]:
        """Count and list distinct projects matching filters, over the whole corpus. Use it for any number or list.

        The result gives the total, counts by programme, by year, by country and (without a policy_area filter) by
        policy area, all computed over every matching project; the project list itself is truncated to `limit`.

        :param countries: ISO country codes (e.g. ["GR"]); a project matches if it involves any of them.
        :param year_from: From this year on.
        :param year_to: Up to this year.
        :param programme: "TSI", "SRSP" or "SRSS" (pre-2017 technical assistance to Greece).
        :param policy_area: Policy area slug (e.g. "health-and-long-term-care").
        :param detail_only: Only projects with a detail page.
        :param title_contains: Case-insensitive word or phrase that must appear in the project title.
        :param limit: Maximum number of projects to list (the total is always given).
        """
        t0 = time.monotonic()
        filters = build_filters(year_from, year_to, programme, policy_area, detail_only, countries=countries)
        projects: dict[str, dict[str, Any]] = {}
        for doc in self.document_store.filter_documents(filters=filters):
            doc_id = doc.meta.get("doc_id")
            if doc_id in projects or not _in_countries(doc, countries):
                continue
            if title_contains and title_contains.lower() not in str(doc.meta.get("title") or "").lower():
                continue
            projects[doc_id] = doc.meta
        ordered = sorted(projects.values(), key=lambda m: (-(m.get("year") or 0), str(m.get("title"))))
        by_programme = Counter(m.get("programme") or "?" for m in ordered)
        by_year = Counter(m.get("year") for m in ordered)
        lines = [f"Total matching projects: {len(ordered)} ("
                 + ", ".join(f"{k}: {v}" for k, v in sorted(by_programme.items())) + ")",
                 "By year: " + ", ".join(f"{y}: {n}" for y, n in sorted(by_year.items(), key=lambda kv: -(kv[0] or 0))),
                 "By country: " + _breakdown(c for m in ordered for c in (m.get("countries") or [None]))]
        if not policy_area:
            lines.append("By policy area: " + _breakdown(m.get("policy_area") for m in ordered))
        lines.append(f"Showing {min(limit, len(ordered))} of {len(ordered)}:")
        for m in ordered[:limit]:
            lines.append(f"- {m.get('title')} ({m.get('doc_id')}) — {m.get('programme')} {m.get('year')} — "
                         f"{', '.join(m.get('countries') or [])} — detail page: "
                         f"{'yes' if m.get('detail_page') else 'no'} — {m.get('source_url')}")
        runlog.event("ProjectLister", "tool_call", "list_projects", countries=countries, filters=filters,
                     title_contains=title_contains, results=len(ordered),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": "\n".join(lines)}


@component
class ProjectReader:
    def __init__(self, document_store: Any, max_chars: int = READER_MAX_CHARS):
        self.document_store = document_store
        self.max_chars = max_chars

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, document_store=self.document_store.to_dict(), max_chars=self.max_chars)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectReader":
        data["init_parameters"]["document_store"] = _deserialize_object(data["init_parameters"]["document_store"])
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict])
    def run(self, doc_id: str, known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Full text of one project (all its sections), by its id as returned by search_projects or list_projects.

        :param doc_id: Project id, e.g. "energy-regulation-academy".
        """
        t0 = time.monotonic()
        docs = self.document_store.filter_documents(
            filters={"field": "meta.doc_id", "operator": "==", "value": doc_id})
        docs.sort(key=lambda d: d.meta.get("chunk_index") or 0)
        if not docs:
            runlog.event("ProjectReader", "tool_call", "get_project", "not_found", doc_id=doc_id)
            return {"result": f"No project with id '{doc_id}'.", "sources": []}
        ref = len(known_sources or []) + 1
        meta = docs[0].meta
        parts = [f"[{ref}] {_label(meta)}\nURL: {meta.get('source_url')}\n"
                 f"Policy area: {meta.get('policy_area')} — detail page: {'yes' if meta.get('detail_page') else 'no'}"]
        current = None
        for doc in docs:
            if doc.meta.get("section") != current:
                current = doc.meta.get("section")
                flag = " (AI-generated summary)" if doc.meta.get("ai_generated") else ""
                parts.append(f"## {current}{flag}")
            parts.append(doc.content or "")
        runlog.event("ProjectReader", "tool_call", "get_project", doc_id=doc_id, chunks=len(docs),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": "\n\n".join(parts)[:self.max_chars], "sources": [_source(ref, docs[0])]}
