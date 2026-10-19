from collections import Counter
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.models import ProjectRef
from tsi_agent.parsers.sitemap import not_modified_since
from tsi_agent.store import ProjectStore, shared_store, StoreEntry
from tsi_agent.urls import is_detail_candidate

NAME = "ChangeDetector"
DROP_RATIO = 0.7      # moins de 70 % des projets connus d'un pays => pays suspect
DROP_MIN_KNOWN = 5    # en dessous, on ne juge pas une chute
PDF_PROGRAMMES = ("SRSP", "SRSS")  # projets lus dans le PDF SRSP (annexe SRSS comprise)


def _in_scope(entry: StoreEntry, scope: dict[str, Any]) -> bool:
    """Le projet peut-il etre dé-listé au vu de ce qui a ete recupere dans ce run ?

    Regle: en scope ssi ses URLs de listing (hors pages détail) sont non vides et toutes
    dans `scope["fetched"]` (les listings effectivement lus ce run) ; sinon on ne sait pas
    s'il manque toujours ailleurs, donc pas de dé-listing. Si `scope` ne porte pas de clé
    "fetched" (anciens appelants/tests), on retombe sur la regle pays/SRSP historique.
    """
    if scope.get("fetched") is None:
        if entry.programme in PDF_PROGRAMMES:
            return bool(scope.get("srsp"))
        if not entry.countries:
            return bool(scope.get("all_countries"))
        return all(c in scope.get("countries", []) for c in entry.countries)
    fetched = set(scope["fetched"])
    listing_urls = [u for u in entry.source_urls if not is_detail_candidate(u)]
    return bool(listing_urls) and all(u in fetched for u in listing_urls)


def _union(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys([*a, *b]))


def _complete(ref: ProjectRef, entry: StoreEntry | None, fetched: set[str]) -> ProjectRef:
    """Complète une vue partielle (run pilote) avec ce que l'état sait des sources non lues dans ce run."""
    if entry is None or not entry.listed:
        return ref
    extra_urls = [u for u in entry.source_urls if not is_detail_candidate(u) and u not in fetched]
    if not extra_urls:
        return ref
    countries = _union(ref.countries, entry.countries)
    return ref.model_copy(update={"listing_urls": _union(ref.listing_urls, extra_urls), "countries": countries,
                                  "multi_country": ref.multi_country or len(countries) > 1})


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
        now = Counter(c for r in refs if r.programme not in PDF_PROGRAMMES for c in r.countries)
        before = Counter(c for e in known.values() if e.listed and e.programme not in PDF_PROGRAMMES
                         for c in e.countries)
        dropped = []
        for country in scope.get("countries", []):
            if before[country] >= DROP_MIN_KNOWN and now[country] < DROP_RATIO * before[country]:
                dropped.append(country)
                runlog.event(NAME, "alert", country, "warning",
                             message=f"{country}: {now[country]} projects listed vs {before[country]} known")
        return dropped

    @staticmethod
    def _covering_owner(ref: ProjectRef, alias_owner: dict[str, str], known: dict[str, StoreEntry], seen: set[str],
                        scope: dict[str, Any]) -> str | None:
        """Projet de l'etat qui couvre ce projet sans page detail (son id ou un de ses alias est un alias connu).

        Couvert si le proprietaire est vu dans ce run, ou si c'est une page detail et que le sitemap n'a pas ete lu
        (`scope["sitemap"]` faux) : sans sitemap, une page detail trouvee seulement par lui n'est pas decouverte,
        ce qui ne prouve pas sa disparition. Sitemap lu et proprietaire absent : page disparue, le thin revient.
        """
        for pid in (ref.project_id, *ref.aliases):
            owner = alias_owner.get(pid)
            if owner is None or owner == ref.project_id:
                continue
            entry = known.get(owner)
            if owner in seen or (entry is not None and entry.detail_page and not scope.get("sitemap")):
                return owner
        return None

    @component.output_types(thin=list[ProjectRef], detail=list[ProjectRef], delisted=list[str], remaining=int,
                            skipped=list[ProjectRef], undated=int)
    def run(self, refs: list[ProjectRef], run_id: str, scope: dict[str, Any],
            suspect_countries: list[str] | None = None, deferred_ids: list[str] | None = None, full: bool = False,
            limit: int | None = None, sitemap_skip: bool = True) -> dict[str, Any]:
        """`skipped` : pages detail connues dont la date du sitemap n'est pas plus recente que celle enregistree
        (mode incremental, sauf `full` ou `sitemap_skip=False`) ; ni telechargees ni comptees dans `limit`, le writer les traite comme
        inchangees (mise a jour du front matter si le listing a change). `undated` : pages detail proposees sans
        date du sitemap (relues a chaque run)."""
        store = self._get_store()
        known = store.all()
        if scope.get("fetched") is not None:
            fetched = set(scope["fetched"])
            refs = [_complete(r, known.get(r.project_id), fetched) for r in refs]
        alias_owner = {alias: pid for pid, e in known.items() for alias in e.aliases}
        offered = self._offered.setdefault(run_id, set())
        thin, detail, skipped, remaining, undated = [], [], [], 0, 0
        deferred = set(deferred_ids or [])
        seen = {r.project_id for r in refs} | {a for r in refs for a in r.aliases} | deferred
        # Lien reporté : il porte l'ancien slug/l'ancienne URL, l'état connaît le projet sous son slug canonique.
        seen |= {pid for pid, e in known.items() if deferred & {*e.aliases, *e.source_urls}}
        for ref in refs:
            entry = known.get(ref.project_id)
            if ref.detail_url:
                if ref.project_id in offered:
                    continue
                if (sitemap_skip and not full and entry is not None and entry.detail_page and entry.content_hash
                        and not_modified_since(ref.sitemap_lastmod, entry.sitemap_lastmod)):
                    offered.add(ref.project_id)
                    skipped.append(ref)
                    runlog.event(NAME, "classify", ref.project_id, "unchanged_sitemap", lastmod=ref.sitemap_lastmod)
                    continue
                if limit is not None and len(detail) >= limit:
                    remaining += 1
                    continue
                offered.add(ref.project_id)
                detail.append(ref)
                undated += ref.sitemap_lastmod is None
                runlog.event(NAME, "classify", ref.project_id, "known_detail" if entry else "new_detail")
            elif (owner := self._covering_owner(ref, alias_owner, known, seen, scope)) is not None:
                # Alias connu d'une page detail : deja couvert par elle, ou page non decouverte dans ce run (sitemap
                # indisponible) sans preuve de sa disparition. Ni ecrit en thin ni dé-liste.
                seen.add(owner)
                runlog.event(NAME, "classify", ref.project_id, "alias_of_detail", owner=owner)
            elif entry is None:
                thin.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "new_thin")
            elif entry.listing_hash != ref.listing_hash() or not entry.listed:
                thin.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "changed_thin")
        suspect = set(suspect_countries or []) | set(self._dropped_countries(refs, known, scope))
        delisted = [
            pid for pid, e in known.items()
            if e.listed and pid not in seen and _in_scope(e, scope) and not (set(e.countries) & suspect)
        ]
        for pid in delisted:
            runlog.event(NAME, "classify", pid, "delisted")
        return {"thin": thin, "detail": detail, "delisted": delisted, "remaining": remaining, "skipped": skipped,
                "undated": undated}
