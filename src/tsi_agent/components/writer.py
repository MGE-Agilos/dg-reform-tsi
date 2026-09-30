from datetime import date
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.models import ProgrammeDoc, ProjectRecord, ProjectRef
from tsi_agent.render import (detail_body, front_matter, parse_front_matter, programme_doc_text, render,
                              source_urls, thin_body)
from tsi_agent.store import ProjectStore, shared_store, StoreEntry

NAME = "MarkdownProjectWriter"
THIN_FLUSH_EVERY = 100  # l'index est réécrit en entier : on groupe les projets thin


@component
class MarkdownProjectWriter:
    """Écrit un fichier par projet et met à jour l'état, projet par projet."""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        self._store: ProjectStore | None = None

    def _get_store(self) -> ProjectStore:
        if self._store is None:
            self._store = shared_store(self.output_dir)
        return self._store

    def _first_seen(self, ref: ProjectRef, today: str) -> str:
        store = self._get_store()
        dates = [e.first_seen for pid in (ref.project_id, *ref.aliases) if (e := store.get(pid))]
        return min(dates) if dates else today

    def _entry(self, ref: ProjectRef, file: str, first_seen: str, today: str,
               content_hash: str | None) -> StoreEntry:
        return StoreEntry(project_id=ref.project_id, file=file, programme=ref.programme,
                          detail_page=ref.detail_page, content_hash=content_hash,
                          listing_hash=ref.listing_hash(), source_urls=source_urls(ref),
                          countries=list(ref.countries), first_seen=first_seen, last_updated=today,
                          listed=True, aliases=list(ref.aliases))

    def _replace_aliases(self, ref: ProjectRef) -> None:
        store = self._get_store()
        for alias in ref.aliases:
            if alias != ref.project_id and store.get(alias):
                store.delete_project_file(alias)
                store.remove(alias)
                runlog.event(NAME, "write", alias, "replaced_by_detail", project_id=ref.project_id)

    def _write_record(self, rec: ProjectRecord, run_id: str, today: str) -> str:
        store = self._get_store()
        ref = rec.ref
        first_seen = self._first_seen(ref, today)
        fm = front_matter(ref, content_hash=rec.content_hash, first_seen=first_seen, last_updated=today,
                          run_id=run_id, model=rec.model)
        file = store.write_project_file(ref.project_id, render(fm, detail_body(ref, rec.content)))
        self._replace_aliases(ref)
        store.put(self._entry(ref, file, first_seen, today, rec.content_hash))
        runlog.event(NAME, "write", ref.project_id, "detail", file=file)
        return file

    def _write_thin(self, ref: ProjectRef, run_id: str, today: str) -> str:
        store = self._get_store()
        first_seen = self._first_seen(ref, today)
        fm = front_matter(ref, content_hash=None, first_seen=first_seen, last_updated=today,
                          run_id=run_id, model="none")
        file = store.write_project_file(ref.project_id, render(fm, thin_body(ref)))
        store.put(self._entry(ref, file, first_seen, today, None), save=False)
        runlog.event(NAME, "write", ref.project_id, "thin", file=file)
        return file

    def _refresh(self, project_id: str, today: str, ref: ProjectRef | None = None, listed: bool = True) -> bool:
        """Met à jour le front matter d'un fichier existant sans rappeler le LLM."""
        store = self._get_store()
        entry = store.get(project_id)
        text = store.read_project_file(project_id)
        if entry is None or text is None:
            return False
        if ref is not None and entry.listing_hash == ref.listing_hash() and entry.listed == listed:
            return False
        old, body = parse_front_matter(text)
        if ref is not None:
            fm = front_matter(ref, content_hash=entry.content_hash, first_seen=entry.first_seen,
                              last_updated=today, run_id=old["extraction"]["run_id"],
                              model=old["extraction"]["model"], listed=listed)
            fm["title"], fm["programme"], fm["year"] = old["title"], old["programme"], old["year"]
            entry.listing_hash, entry.countries, entry.source_urls = ref.listing_hash(), list(ref.countries), source_urls(ref)
        else:
            fm = {**old, "listed": listed, "last_updated": today}
        store.write_project_file(project_id, render(fm, body))
        entry.listed, entry.last_updated = listed, today
        store.put(entry)
        return True

    @component.output_types(written=list[str], stats=dict[str, int])
    def run(self, run_id: str, records: list[ProjectRecord] | None = None, thin: list[ProjectRef] | None = None,
            unchanged: list[ProjectRef] | None = None, delisted: list[str] | None = None,
            programme_docs: list[ProgrammeDoc] | None = None) -> dict[str, Any]:
        today = date.today().isoformat()
        store = self._get_store()
        written: list[str] = []
        stats = {"detail": 0, "thin": 0, "refreshed": 0, "delisted": 0, "programme_docs": 0}
        for rec in records or []:
            written.append(self._write_record(rec, run_id, today))
            stats["detail"] += 1
        for i, ref in enumerate(thin or [], start=1):
            written.append(self._write_thin(ref, run_id, today))
            stats["thin"] += 1
            if i % THIN_FLUSH_EVERY == 0:
                store.flush()
        store.flush()
        for ref in unchanged or []:
            if self._refresh(ref.project_id, today, ref=ref):
                stats["refreshed"] += 1
                runlog.event(NAME, "write", ref.project_id, "refreshed_front_matter")
        for pid in delisted or []:
            if self._refresh(pid, today, listed=False):
                stats["delisted"] += 1
                runlog.event(NAME, "delist", pid)
        for doc in programme_docs or []:
            if store.programme_hash(doc.doc_id) != doc.content_hash:
                written.append(store.write_programme_doc(doc.doc_id, programme_doc_text(doc, run_id, today),
                                                         doc.content_hash))
                stats["programme_docs"] += 1
                runlog.event(NAME, "write", doc.doc_id, "programme_doc")
        return {"written": written, "stats": stats}
