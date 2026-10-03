"""Outils de l'agent chatbot : recherche semantique (projets, texte integral des livrables), liste/comptage
de projets, lecture d'un projet.

Composants Haystack exposes via ComponentTool. Les filtres annee/programme/domaine/detail passent par les
filtres Haystack standards (meme comportement InMemory et OpenSearch) ; le filtre pays est applique ici en
Python, un filtre sur un champ liste n'ayant pas la meme semantique selon le store.
"""

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


def build_filters(year_from: int | None = None, year_to: int | None = None, programme: str | None = None,
                  policy_area: str | None = None, detail_only: bool = False,
                  doc_type: str | list[str] | None = "project") -> dict[str, Any] | None:
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

        :param query: What to look for, in any language (e.g. "reform of the healthcare system").
        :param countries: ISO country codes to keep (e.g. ["HR", "SI"]; Greece is "GR").
        :param year_from: Keep projects from this year on (e.g. 2022).
        :param year_to: Keep projects up to this year.
        :param programme: "TSI" (2021 onwards) or "SRSP" (2017-2020).
        :param policy_area: Policy area slug (e.g. "green-transition").
        :param detail_only: Keep only projects that have a detail page (richer descriptions).
        :param include_programme: Also search country factsheets and policy area pages (programme figures).
        """
        t0 = time.monotonic()
        embedding = self.text_embedder.run(text=query)["embedding"]
        filters = build_filters(year_from, year_to, programme, policy_area, detail_only,
                                doc_type=["project", "programme"] if include_programme else "project")
        candidates = SEARCH_CANDIDATES_WITH_FILTER if countries else self.top_k * 3
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

        :param query: What to look for, in any language (e.g. "survey sample size and response rate").
        :param countries: ISO country codes of the related projects (e.g. ["HR", "SI"]; Greece is "GR").
        :param year_from: Keep deliverables of projects from this year on.
        :param year_to: Keep deliverables of projects up to this year.
        :param programme: "TSI" (2021 onwards) or "SRSP" (2017-2020).
        :param policy_area: Policy area slug (e.g. "green-transition").
        :param publication_id: Keep only this publication (id as returned in earlier results).
        :param project_id: Keep only deliverables linked to this project id.
        """
        t0 = time.monotonic()
        embedding = self.text_embedder.run(text=query)["embedding"]
        filters = build_filters(year_from, year_to, programme, policy_area, doc_type="deliverable")
        if publication_id:
            filters["conditions"].append({"field": "meta.doc_id", "operator": "==", "value": publication_id})
        candidates = SEARCH_CANDIDATES_WITH_FILTER if (countries or project_id) else self.top_k * 3
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

        :param countries: ISO country codes (e.g. ["GR"]); a project matches if it involves any of them.
        :param year_from: From this year on.
        :param year_to: Up to this year.
        :param programme: "TSI" or "SRSP".
        :param policy_area: Policy area slug (e.g. "health-and-long-term-care").
        :param detail_only: Only projects with a detail page.
        :param title_contains: Case-insensitive word or phrase that must appear in the project title.
        :param limit: Maximum number of projects to list (the total is always given).
        """
        t0 = time.monotonic()
        filters = build_filters(year_from, year_to, programme, policy_area, detail_only)
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
                 f"Showing {min(limit, len(ordered))} of {len(ordered)}:"]
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
    def __init__(self, document_store: Any):
        self.document_store = document_store

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, document_store=self.document_store.to_dict())

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
        return {"result": "\n\n".join(parts)[:READER_MAX_CHARS], "sources": [_source(ref, docs[0])]}
