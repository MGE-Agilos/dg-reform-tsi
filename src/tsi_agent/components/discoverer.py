import hashlib
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, name_from_iso, slug_from_iso
from tsi_agent.fuzzy import FuzzyMatch, countries_in_text, match_thin_refs
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import slug_from_url, title_key
from tsi_agent.models import ProgrammeDoc, ProjectRef, RawPage
from tsi_agent.parsers import main_text
from tsi_agent.parsers.detail import parse_detail, parse_detail_meta
from tsi_agent.parsers.factsheet import parse_factsheet
from tsi_agent.parsers.policy_area import parse_policy_area
from tsi_agent.parsers.sitemap import DEFAULT_EXCLUDED_AREAS, parse_sitemap, sitemap_lastmods, sitemap_project_urls
from tsi_agent.parsers.spotlight import parse_spotlight
from tsi_agent.parsers.srsp_pdf import parse_srsp_pdf
from tsi_agent.render import parse_front_matter
from tsi_agent.store import ProjectStore, shared_store
from tsi_agent.urls import is_canonical_detail, is_final_detail, policy_area_from_url

NAME = "ProjectDiscoverer"
MIN_PROGRAMME_TEXT = 300


def ref_title_key(ref: ProjectRef) -> str | None:
    return title_key(ref.programme, ref.year, ref.title) if ref.programme and ref.year else None


def _union(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys([*a, *b]))


def combine(a: ProjectRef, b: ProjectRef) -> ProjectRef:
    """Fusionne deux vues du même projet."""
    detail_url = a.detail_url or b.detail_url
    title = a.title if a.detail_url or not b.detail_url else b.title
    countries = _union(a.countries, b.countries)
    aliases = _union(a.aliases, b.aliases)
    if detail_url:
        aliases = _union(aliases, [r.project_id for r in (a, b) if not r.detail_url])
    project_id = slug_from_url(detail_url) if detail_url else a.project_id
    return ProjectRef(
        project_id=project_id, title=title, listing_titles=_union(a.listing_titles, b.listing_titles),
        programme=a.programme or b.programme, year=a.year or b.year,
        countries=countries, multi_country=a.multi_country or b.multi_country or len(countries) > 1,
        detail_url=detail_url, policy_area=a.policy_area or b.policy_area,
        spotlight=a.spotlight or b.spotlight, highlighted=a.highlighted or b.highlighted,
        listing_urls=_union(a.listing_urls, b.listing_urls),
        aliases=[x for x in aliases if x != project_id],
        sitemap_lastmod=(a if a.detail_url else b).sitemap_lastmod,  # date de la page detail gardee
    )


def _detail_urls_by_key(refs: list[ProjectRef]) -> dict[str, set[str]]:
    urls_by_key: dict[str, set[str]] = {}
    for ref in refs:
        key = ref_title_key(ref)
        if key and ref.detail_url:
            urls_by_key.setdefault(key, set()).add(ref.detail_url)
    return urls_by_key


def ambiguous_title_keys(refs: list[ProjectRef]) -> set[str]:
    """Cles titre associees a plus d'une page detail (titre "flagship" generique, une page par pays)."""
    return {k for k, urls in _detail_urls_by_key(refs).items() if len(urls) > 1}


def merge_refs(refs: list[ProjectRef]) -> list[ProjectRef]:
    """Même projet = même URL détail ou même clé titre (programme, année, titre), transitivement.

    Union-find sur les indices d'entrée. Deux refs sont liées si elles partagent une URL détail
    ou une clé titre ; le lien se propage (si A~B par URL et B~C par clé, alors A~B~C). Garde-fou :
    on ne fusionne jamais deux groupes dont les URLs détail distinctes combinées seraient > 1
    (deux pages détail différentes = deux projets différents, même si la clé titre correspond).

    Cas des titres "flagship" partagés par plusieurs pays (même clé titre générique, mais chaque
    pays pointe vers SA propre page détail) : une clé titre associée à > 1 URL détail distinctes
    est dite ambiguë. Pour une clé ambiguë, les refs avec page détail ne se lient plus par la clé
    (seulement par URL) ; seules les refs sans page détail (thin) se lient entre elles, sous un
    espace de noms séparé, pour former le projet "thin" distinct des pages détail des pays.
    """
    n = len(refs)
    parent = list(range(n))
    members: dict[int, set[int]] = {i: {i} for i in range(n)}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def detail_urls(root: int) -> set[str]:
        return {refs[m].detail_url for m in members[root] if refs[m].detail_url}

    urls_by_key = _detail_urls_by_key(refs)
    ambiguous_keys = {k for k, urls in urls_by_key.items() if len(urls) > 1}
    for k in sorted(ambiguous_keys):
        runlog.event(NAME, "discover", k, "ambiguous_title", urls=sorted(urls_by_key[k]))

    anchors: dict[tuple[str, str], int] = {}
    for i, ref in enumerate(refs):
        link_keys: list[tuple[str, str]] = []
        if ref.detail_url:
            link_keys.append(("url", ref.detail_url))
        key = ref_title_key(ref)
        if key:
            if key not in ambiguous_keys:
                link_keys.append(("key", key))
            elif not ref.detail_url:
                link_keys.append(("thin-key", key))
        for link_key in link_keys:
            other = anchors.setdefault(link_key, i)
            if other == i:
                continue
            ri, rj = find(i), find(other)
            if ri == rj:
                continue
            combined = detail_urls(ri) | detail_urls(rj)
            if len(combined) > 1:
                runlog.event(NAME, "discover", link_key[1], "merge_conflict", urls=sorted(combined))
                continue
            new_root, old_root = (ri, rj) if ri < rj else (rj, ri)
            parent[old_root] = new_root
            members[new_root] |= members[old_root]
            del members[old_root]

    groups: list[ProjectRef] = []
    for root in sorted(members):
        idxs = sorted(members[root])
        merged = refs[idxs[0]].model_copy(deep=True)
        for idx in idxs[1:]:
            merged = combine(merged, refs[idx])
        if merged.detail_url and ambiguous_keys:
            merged.aliases = [a for a in merged.aliases if a not in ambiguous_keys]
        groups.append(merged)
    return groups


def fuzzy_merge(refs: list[ProjectRef], views: list[ProjectRef] | None = None,
                excluded_thin_ids: set[str] | None = None) -> tuple[list[ProjectRef], list[FuzzyMatch]]:
    """Rattache les projets thin a la page detail du meme projet (regle floue de tsi_agent.fuzzy).

    Le projet fusionne garde l'id de sa page detail ; l'id du thin passe dans ses alias (le writer
    supprime alors le fichier thin, comme pour les fusions par cle titre).
    """
    matches = match_thin_refs(refs, views, excluded_thin_ids=excluded_thin_ids)
    absorbed = {m.thin_index: m.detail_index for m in matches}
    by_detail = {m.detail_index: m.thin_index for m in matches}
    out: list[ProjectRef] = []
    for i, ref in enumerate(refs):
        if i in absorbed:
            continue
        out.append(combine(ref, refs[by_detail[i]]) if i in by_detail else ref)
    for m in matches:
        runlog.event(NAME, "discover", m.detail_id, "fuzzy_merge", thin_id=m.thin_id, score=round(m.score, 3))
    return out, matches


@component
class ProjectDiscoverer:
    """Parse les listings, résout les liens non canoniques, fusionne, produit les documents programme."""

    def __init__(self, cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT, sitemap_excluded_areas: list[str] | None = None,
                 output_dir: str | None = None):
        """:param sitemap_excluded_areas: rubriques du sitemap qui ne sont pas des pages projet (defaut :
        DEFAULT_EXCLUDED_AREAS).
        :param output_dir: dossier de l'etat ; titre, programme et annee officiels d'une page detail absente du
        cache HTTP (non relue en mode incremental) y sont lus pour la fusion floue. None : pas d'etat consulte."""
        self.output_dir = output_dir
        self._store: ProjectStore | None = None
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.sitemap_excluded_areas = (list(sitemap_excluded_areas) if sitemap_excluded_areas is not None
                                       else list(DEFAULT_EXCLUDED_AREAS))
        self._fetcher: PoliteFetcher | None = None
        # Mémo par instance (donc par process) : évite de rappeler le fetcher ou le parseur PDF
        # pour la même URL/le même contenu à chaque lot d'un même run.
        self._resolve_cache: dict[str, tuple[str, str | None]] = {}
        self._srsp_cache: dict[str, list[ProjectRef]] = {}
        # Page du sitemap -> ("ref", ProjectRef) | ("not_project", None) | ("failed", erreur)
        self._sitemap_cache: dict[str, tuple[str, Any]] = {}
        self._hint_cache: dict[str, tuple[str | None, int | None, str | None]] = {}

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    def _resolve(self, ref: ProjectRef, deferred: list[str]) -> ProjectRef | None:
        """Suit les redirections d'un lien non canonique.

        None si le projet est abandonné (lien mort sans clé titre) ou reporté (échec temporaire :
        ses ids vont dans `deferred` pour qu'il ne soit ni écrit en thin ni dé-listé).
        """
        if ref.detail_url is None or is_canonical_detail(ref.detail_url):
            return ref
        url = ref.detail_url
        outcome = self._resolve_cache.get(url)
        if outcome is None:
            try:
                outcome = ("resolved", self._get_fetcher().get(url).final_url)
            except FetchError as exc:
                permanent = exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 429)
                outcome = ("resolved", None) if permanent else ("deferred", str(exc))
            self._resolve_cache[url] = outcome
        kind, value = outcome
        if kind == "deferred":
            deferred.extend([ref.project_id, *ref.aliases, ref.detail_url])
            runlog.event(NAME, "resolve", ref.detail_url, "deferred", error=value)
            return None
        final = value
        if final and is_final_detail(final):
            runlog.event(NAME, "resolve", ref.detail_url, "ok", final_url=final)
            return ref.model_copy(update={"detail_url": final, "project_id": slug_from_url(final)})
        key = ref_title_key(ref)
        if key is None:
            runlog.event(NAME, "resolve", ref.detail_url, "dropped")
            return None
        runlog.event(NAME, "resolve", ref.detail_url, "thin", final_url=final)
        return ref.model_copy(update={"detail_url": None, "project_id": key,
                                      "aliases": [a for a in ref.aliases if a != key]})

    @component.output_types(refs=list[ProjectRef], programme_docs=list[ProgrammeDoc],
                            suspect_countries=list[str], scope=dict[str, Any], deferred_ids=list[str],
                            report=dict[str, Any], excluded_thin_ids=list[str])
    def run(self, pages: list[RawPage]) -> dict[str, Any]:
        refs: list[ProjectRef] = []
        docs: list[ProgrammeDoc] = []
        suspect: list[str] = []
        scope: dict[str, Any] = {"countries": [], "srsp": False, "all_countries": False}
        sitemap_urls: list[str] = []
        sitemap_keys: set[str] = set()
        lastmods: dict[str, str] = {}
        for page in pages:
            if page.kind == "sitemap":
                try:
                    sitemap_urls += parse_sitemap(page.content)[1]
                    lastmods.update(sitemap_lastmods(page.content))
                    sitemap_keys.add(page.key)
                except ValueError as exc:
                    runlog.event(NAME, "error", page.url, "parse_failed", kind=page.kind, error=repr(exc))
                continue
            try:
                found = self._parse(page, docs, scope)
            except Exception as exc:  # une page cassée ne doit pas arrêter le run
                runlog.event(NAME, "error", page.url, "parse_failed", kind=page.kind, error=repr(exc))
                continue
            if page.kind == "factsheet" and not found:
                suspect.append(page.key)
                runlog.event(NAME, "alert", page.key, "warning",
                             message=f"Factsheet {page.key}: 0 project parsed (site structure changed?)")
            refs += found
        scope["all_countries"] = set(scope["countries"]) == set(COUNTRIES)
        scope["sitemap"] = bool(sitemap_keys)  # sitemap complet lu : les pages detail qu'il liste sont decouvertes
        # Sources lues dans ce run (vue partielle ?) ; un projet trouve par le sitemap a pour source l'index.
        scope["fetched"] = sorted({page.url for page in pages} | sitemap_keys)
        deferred: list[str] = []
        resolved: list[ProjectRef] = []
        for r in refs:
            try:
                result = self._resolve(r, deferred)
            except Exception as exc:  # une erreur inattendue ne doit ni planter le run ni perdre le projet
                runlog.event(NAME, "resolve", r.detail_url, "deferred", error=repr(exc))
                deferred.extend([r.project_id, *r.aliases, *([r.detail_url] if r.detail_url else [])])
                continue
            if result is not None:
                resolved.append(result)
        project_urls = sitemap_project_urls(sitemap_urls, self.sitemap_excluded_areas)
        sitemap_report: dict[str, Any] = {"urls": len(project_urls), "new_urls": 0, "fetched": 0,
                                          "not_project_pages": [], "failed": [], "deferred": []}
        from_sitemap: list[ProjectRef] = []
        if sitemap_keys:
            from_sitemap = self._sitemap_refs(project_urls, {r.detail_url for r in resolved if r.detail_url},
                                              sorted(sitemap_keys)[0], deferred, sitemap_report)
        thin_ids = {r.project_id for r in resolved if not r.detail_url}
        resolved += from_sitemap
        for r in resolved:
            if r.detail_url and not r.policy_area:
                r.policy_area = policy_area_from_url(r.detail_url)
        ambiguous = ambiguous_title_keys(resolved)
        merged, fuzzy = self._fuzzy_with_hints(self._merge_known_aliases(merge_refs(resolved), ambiguous), ambiguous)
        for r in merged:  # mode incremental : date du sitemap de la page detail (None sans sitemap ou sans date)
            r.sitemap_lastmod = lastmods.get(r.detail_url) if r.detail_url else None
        report = self._report(merged, fuzzy, thin_ids, {r.detail_url for r in from_sitemap}, sitemap_report)
        kept_docs = []
        for doc in docs:
            if len(doc.text) < MIN_PROGRAMME_TEXT:
                runlog.event(NAME, "alert", doc.doc_id, "warning",
                             message=f"Programme document {doc.doc_id} has no content ({len(doc.text)} chars)")
            else:
                kept_docs.append(doc)
        runlog.event(NAME, "discover", None, "ok", raw_refs=len(refs), projects=len(merged),
                     detail=sum(r.detail_page for r in merged), programme_docs=len(kept_docs))
        return {"refs": merged, "programme_docs": kept_docs, "suspect_countries": suspect, "scope": scope,
                "deferred_ids": deferred, "report": report, "excluded_thin_ids": sorted(ambiguous)}

    # --- sitemap -----------------------------------------------------------------------------------------------
    def _sitemap_ref(self, url: str, listing_url: str) -> tuple[str, Any]:
        """Page detail du sitemap -> ProjectRef (titre, programme, annee et domaine lus sur la page et l'URL)."""
        try:
            result = self._get_fetcher().get(url)
        except FetchError as exc:
            permanent = exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 429)
            return ("failed" if permanent else "deferred", str(exc))
        except OSError as exc:
            return ("deferred", repr(exc))
        final = result.final_url if is_final_detail(result.final_url) else url
        parsed = parse_detail(result.content)
        if parsed.programme is None and parsed.year is None:
            return ("not_project", None)  # ni "Funding Programme" ni "Year" : page de rubrique, pas un projet
        # Pays laisses vides : la page detail n'en donne pas, le LLM les lit dans le texte (comme pour les
        # pages detail des domaines politiques).
        return ("ref", ProjectRef(project_id=slug_from_url(final), title=parsed.title or slug_from_url(final),
                                  programme=parsed.programme, year=parsed.year, detail_url=final,
                                  policy_area=policy_area_from_url(final), listing_urls=[listing_url]))

    def _sitemap_refs(self, urls: list[str], known: set[str], listing_url: str, deferred: list[str],
                      report: dict[str, Any]) -> list[ProjectRef]:
        """Projets des pages du sitemap qui ne sont la page detail d'aucun projet deja decouvert."""
        new_urls = [u for u in urls if u not in known]
        report["new_urls"] = len(new_urls)
        out = []
        for url in new_urls:
            outcome = self._sitemap_cache.get(url)
            if outcome is None:
                try:
                    outcome = self._sitemap_ref(url, listing_url)
                except Exception as exc:  # une page cassee ne doit pas arreter le run
                    outcome = ("deferred", repr(exc))
                if outcome[0] != "deferred":  # echec temporaire : on reessaiera au lot suivant
                    self._sitemap_cache[url] = outcome
                extra = {"error": outcome[1]} if outcome[0] in ("failed", "deferred") else {}
                runlog.event(NAME, "sitemap", url, outcome[0], **extra)
            kind, value = outcome
            if kind == "ref":
                report["fetched"] += 1
                if value.detail_url in known:
                    continue  # redirigee vers une page deja connue
                known.add(value.detail_url)
                out.append(value.model_copy(deep=True))
            elif kind == "not_project":
                report["fetched"] += 1
                report["not_project_pages"].append(url)
            elif kind == "failed":
                report["failed"].append(url)
            else:
                report["deferred"].append(url)
                deferred.extend([slug_from_url(url), url])
        return out

    # --- alias connus ---------------------------------------------------------------------------------------------
    def _get_store(self) -> ProjectStore | None:
        if self._store is None and self.output_dir is not None:
            self._store = shared_store(self.output_dir)
        return self._store

    def _merge_known_aliases(self, refs: list[ProjectRef], ambiguous: set[str]) -> list[ProjectRef]:
        """Rattache a sa page detail un projet sans page detail dont l'id est un alias connu de cette page dans l'etat.

        Fusion deterministe, avant la fusion floue et l'arbitre : elle ne depend ni des indices (cache HTTP, titres
        officiels), ni du LLM. Si la page detail n'est pas decouverte dans ce run (sitemap indisponible), le projet
        sans page detail reste tel quel : le detecteur ne l'ecrit pas tant que la disparition de la page n'est pas
        etablie. Les cles titre ambigues (projet "flagship" distinct des pages pays) ne sont jamais rattachees.
        """
        store = self._get_store()
        if store is None:
            return refs
        owner_of = {alias: pid for pid, e in store.all().items() if e.detail_page for alias in e.aliases
                    if alias != pid}
        if not owner_of:
            return refs
        detail_index = {r.project_id: i for i, r in enumerate(refs) if r.detail_url}
        absorbed: dict[int, list[int]] = {}
        for i, ref in enumerate(refs):
            if ref.detail_url:
                continue
            owner = next((owner_of[x] for x in (ref.project_id, *ref.aliases)
                          if x in owner_of and x not in ambiguous), None)
            if owner is None or owner not in detail_index:
                continue
            absorbed.setdefault(detail_index[owner], []).append(i)
            runlog.event(NAME, "discover", owner, "alias_merge", thin_id=ref.project_id)
        if not absorbed:
            return refs
        dropped = {i for idxs in absorbed.values() for i in idxs}
        out = []
        for i, ref in enumerate(refs):
            if i in dropped:
                continue
            for j in absorbed.get(i, []):
                ref = combine(ref, refs[j])
            out.append(ref)
        return out

    # --- fusion floue --------------------------------------------------------------------------------------------
    def _hint_from_store(self, url: str) -> tuple[str | None, int | None, str | None]:
        """Programme, annee et titre officiel enregistres pour cette page detail (etat, sinon front matter du
        fichier projet pour un etat anterieur a ces champs) ; (None, None, None) si inconnue ou sans etat."""
        store = self._get_store()
        if store is None:
            return (None, None, None)
        entry = store.get(slug_from_url(url))
        if entry is None or not entry.detail_page or url not in entry.source_urls:
            return (None, None, None)
        if entry.title:
            return (entry.programme, entry.year, entry.title)
        text = store.read_project_file(entry.project_id)
        if text is None:
            return (None, None, None)
        fm, _ = parse_front_matter(text.replace("\r\n", "\n"))
        year = fm.get("year")
        return (fm.get("programme"), int(year) if year else None, fm.get("title") or None)

    def _hint(self, url: str) -> tuple[str | None, int | None, str | None]:
        """Programme, annee et titre officiel d'une page detail : cache HTTP, sinon etat (aucune requete)."""
        if url not in self._hint_cache:
            hint: tuple[str | None, int | None, str | None] = (None, None, None)
            try:
                content = self._get_fetcher().cached(url)
                if content is not None:
                    title, programme, year = parse_detail_meta(content)
                    hint = (programme, year, title or None)
                else:
                    hint = self._hint_from_store(url)
            except Exception as exc:  # indice seulement : une page illisible ne change rien
                runlog.event(NAME, "discover", url, "hint_failed", error=repr(exc))
            self._hint_cache[url] = hint
        return self._hint_cache[url]

    def _fuzzy_with_hints(self, refs: list[ProjectRef], ambiguous: set[str]
                          ) -> tuple[list[ProjectRef], list[FuzzyMatch]]:
        """Fusion floue ; les controles utilisent des vues, pas les refs (qui ne changent pas).

        Vue d'une page detail en cache HTTP : son titre officiel (et non un titre de listing, parfois generique
        ou propre a un pays d'un projet multi-pays), son programme et son annee ; pays deduits du titre a defaut.
        """
        views = []
        for ref in refs:
            update: dict[str, Any] = {}
            if ref.detail_url:
                programme, year, title = self._hint(ref.detail_url)
                update.update({"programme": ref.programme or programme, "year": ref.year or year,
                               "title": title or ref.title})
                if not ref.countries:
                    update["countries"] = countries_in_text(update["title"])
            views.append(ref.model_copy(update=update) if update else ref)
        return fuzzy_merge(refs, views, excluded_thin_ids=ambiguous)

    @staticmethod
    def _report(refs: list[ProjectRef], fuzzy: list[FuzzyMatch], thin_ids: set[str], sitemap_urls: set[str],
                sitemap: dict[str, Any]) -> dict[str, Any]:
        """Bilan de la decouverte pour la revue humaine (--dry-run)."""
        fuzzy_by_detail = {m.detail_id: m for m in fuzzy}
        merged_into_thin, new_projects, duplicates = [], [], []
        for ref in refs:
            fm = fuzzy_by_detail.get(ref.project_id)
            if ref.detail_url in sitemap_urls:
                exact = [a for a in ref.aliases if a in thin_ids and (fm is None or a != fm.thin_id)]
                pairs = [(a, 1.0, "title_key") for a in exact] + ([(fm.thin_id, fm.score, "fuzzy")] if fm else [])
                for thin_id, score, rule in pairs:
                    merged_into_thin.append({"sitemap_slug": ref.project_id, "thin_id": thin_id,
                                             "score": round(score, 3), "rule": rule, "title": ref.title,
                                             "programme": ref.programme, "year": ref.year})
                if not pairs:
                    new_projects.append({"project_id": ref.project_id, "title": ref.title,
                                         "programme": ref.programme, "year": ref.year, "url": ref.detail_url})
            elif fm is not None:
                duplicates.append({"detail_id": fm.detail_id, "thin_id": fm.thin_id, "score": round(fm.score, 3)})
        merged_into_thin.sort(key=lambda m: (-m["score"], m["sitemap_slug"], m["thin_id"]))
        return {"refs": len(refs), "detail": sum(r.detail_page for r in refs), "sitemap": sitemap,
                "merged_into_thin": merged_into_thin, "new_projects": new_projects,
                "duplicates_merged": duplicates}

    def _parse(self, page: RawPage, docs: list[ProgrammeDoc], scope: dict[str, Any]) -> list[ProjectRef]:
        if page.kind == "factsheet":
            parsed = parse_factsheet(page.content, page.url, page.key)
            scope["countries"].append(page.key)
            for year, announced in parsed.announced.items():
                if announced is not None and announced != parsed.parsed_counts.get(year):
                    runlog.event(NAME, "discover", page.key, "count_mismatch", year=year, announced=announced,
                                 parsed=parsed.parsed_counts.get(year))
            docs.append(ProgrammeDoc(doc_id=f"country-{slug_from_iso(page.key)}",
                                     title=f"TSI country factsheet - {name_from_iso(page.key)}",
                                     url=page.url, text=parsed.programme_text))
            return parsed.refs
        if page.kind == "spotlight":
            return parse_spotlight(page.content, page.url)
        if page.kind == "policy_area":
            found, text = parse_policy_area(page.content, page.url)
            docs.append(ProgrammeDoc(doc_id=f"policy-area-{page.key}", title=f"TSI policy area - {page.key}",
                                     url=page.url, text=text))
            return found
        if page.kind == "srsp_pdf":
            scope["srsp"] = True
            key = hashlib.sha256(page.content).hexdigest()
            cached = self._srsp_cache.get(key)
            if cached is None:
                cached = parse_srsp_pdf(page.content, page.url)
                self._srsp_cache[key] = cached
            return [r.model_copy(deep=True) for r in cached]
        docs.append(ProgrammeDoc(doc_id=page.key, title=page.title or page.key, url=page.url,
                                 text=main_text(page.content)))
        return []
