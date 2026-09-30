from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, name_from_iso, slug_from_iso
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import slug_from_url, title_key
from tsi_agent.models import ProgrammeDoc, ProjectRef, RawPage
from tsi_agent.parsers import main_text
from tsi_agent.parsers.factsheet import parse_factsheet
from tsi_agent.parsers.policy_area import parse_policy_area
from tsi_agent.parsers.spotlight import parse_spotlight
from tsi_agent.parsers.srsp_pdf import parse_srsp_pdf
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
        project_id=project_id, title=title, programme=a.programme or b.programme, year=a.year or b.year,
        countries=countries, multi_country=a.multi_country or b.multi_country or len(countries) > 1,
        detail_url=detail_url, policy_area=a.policy_area or b.policy_area,
        spotlight=a.spotlight or b.spotlight, highlighted=a.highlighted or b.highlighted,
        listing_urls=_union(a.listing_urls, b.listing_urls),
        aliases=[x for x in aliases if x != project_id],
    )


def merge_refs(refs: list[ProjectRef]) -> list[ProjectRef]:
    """Même projet = même URL détail ou même clé titre (programme, année, titre)."""
    groups: list[ProjectRef] = []
    by_url: dict[str, int] = {}
    by_key: dict[str, int] = {}
    for ref in refs:
        key = ref_title_key(ref)
        idx = by_url.get(ref.detail_url) if ref.detail_url else None
        if idx is None and key:
            idx = by_key.get(key)
        if idx is None:
            groups.append(ref.model_copy(deep=True))
            idx = len(groups) - 1
        else:
            groups[idx] = combine(groups[idx], ref)
        merged = groups[idx]
        if merged.detail_url:
            by_url[merged.detail_url] = idx
        for k in (key, ref_title_key(merged)):
            if k:
                by_key[k] = idx
    return groups


@component
class ProjectDiscoverer:
    """Parse les listings, résout les liens non canoniques, fusionne, produit les documents programme."""

    def __init__(self, cache_dir: str = ".cache/http", min_interval_s: float = 1.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher: PoliteFetcher | None = None

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
        try:
            final = self._get_fetcher().get(ref.detail_url).final_url
        except FetchError as exc:
            if exc.status not in (404, 410):
                deferred.extend([ref.project_id, *ref.aliases])
                runlog.event(NAME, "resolve", ref.detail_url, "deferred", error=str(exc))
                return None
            final = None
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
                            suspect_countries=list[str], scope=dict[str, Any], deferred_ids=list[str])
    def run(self, pages: list[RawPage]) -> dict[str, Any]:
        refs: list[ProjectRef] = []
        docs: list[ProgrammeDoc] = []
        suspect: list[str] = []
        scope: dict[str, Any] = {"countries": [], "srsp": False, "all_countries": False}
        for page in pages:
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
        deferred: list[str] = []
        resolved = [r for r in (self._resolve(r, deferred) for r in refs) if r is not None]
        for r in resolved:
            if r.detail_url and not r.policy_area:
                r.policy_area = policy_area_from_url(r.detail_url)
        merged = merge_refs(resolved)
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
                "deferred_ids": deferred}

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
            return parse_srsp_pdf(page.content, page.url)
        docs.append(ProgrammeDoc(doc_id=page.key, title=page.title or page.key, url=page.url,
                                 text=main_text(page.content)))
        return []
