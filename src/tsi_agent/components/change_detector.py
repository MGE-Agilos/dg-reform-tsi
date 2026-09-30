from collections import Counter
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.models import ProjectRef
from tsi_agent.store import ProjectStore, shared_store, StoreEntry

NAME = "ChangeDetector"
DROP_RATIO = 0.7      # moins de 70 % des projets connus d'un pays => pays suspect
DROP_MIN_KNOWN = 5    # en dessous, on ne juge pas une chute


def _in_scope(entry: StoreEntry, scope: dict[str, Any]) -> bool:
    if entry.programme == "SRSP":
        return bool(scope.get("srsp"))
    if not entry.countries:
        return bool(scope.get("all_countries"))
    return any(c in scope.get("countries", []) for c in entry.countries)


@component
class ChangeDetector:
    """Compare les projets découverts à l'état : que faut-il (ré)extraire, écrire, dé-lister ?"""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        self._store: ProjectStore | None = None
        self._offered: dict[str, set[str]] = {}  # run_id -> projets détaillés déjà proposés dans ce run

    def _get_store(self) -> ProjectStore:
        if self._store is None:
            self._store = shared_store(self.output_dir)
        return self._store

    def _dropped_countries(self, refs: list[ProjectRef], known: dict[str, StoreEntry],
                           scope: dict[str, Any]) -> list[str]:
        now = Counter(c for r in refs if r.programme != "SRSP" for c in r.countries)
        before = Counter(c for e in known.values() if e.listed and e.programme != "SRSP" for c in e.countries)
        dropped = []
        for country in scope.get("countries", []):
            if before[country] >= DROP_MIN_KNOWN and now[country] < DROP_RATIO * before[country]:
                dropped.append(country)
                runlog.event(NAME, "alert", country, "warning",
                             message=f"{country}: {now[country]} projects listed vs {before[country]} known")
        return dropped

    @component.output_types(thin=list[ProjectRef], detail=list[ProjectRef], delisted=list[str], remaining=int)
    def run(self, refs: list[ProjectRef], run_id: str, scope: dict[str, Any],
            suspect_countries: list[str] | None = None, deferred_ids: list[str] | None = None, full: bool = False,
            limit: int | None = None) -> dict[str, Any]:
        store = self._get_store()
        known = store.all()
        alias_owner = {alias: pid for pid, e in known.items() for alias in e.aliases}
        offered = self._offered.setdefault(run_id, set())
        thin, detail, remaining = [], [], 0
        for ref in refs:
            entry = known.get(ref.project_id)
            if ref.detail_url:
                if ref.project_id in offered:
                    continue
                if limit is not None and len(detail) >= limit:
                    remaining += 1
                    continue
                offered.add(ref.project_id)
                detail.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "known_detail" if entry else "new_detail")
            elif entry is None:
                if ref.project_id in alias_owner:
                    continue  # déjà couvert par sa page détail
                thin.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "new_thin")
            elif entry.listing_hash != ref.listing_hash() or not entry.listed:
                thin.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "changed_thin")
        suspect = set(suspect_countries or []) | set(self._dropped_countries(refs, known, scope))
        seen = {r.project_id for r in refs} | {a for r in refs for a in r.aliases} | set(deferred_ids or [])
        delisted = [
            pid for pid, e in known.items()
            if e.listed and pid not in seen and _in_scope(e, scope) and not (set(e.countries) & suspect)
        ]
        for pid in delisted:
            runlog.event(NAME, "classify", pid, "delisted")
        return {"thin": thin, "detail": detail, "delisted": delisted, "remaining": remaining}
