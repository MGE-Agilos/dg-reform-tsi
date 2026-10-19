import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, slug_from_iso
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.models import RawPage
from tsi_agent.parsers.sitemap import parse_sitemap, sitemap_lastmods

NAME = "SourceListingFetcher"
SNAPSHOT_PATH = "state/sitemap_snapshot.json"  # sous output_dir : derniere lecture complete du sitemap
STATUS_PATH = "state/sitemap_status.json"  # sous output_dir : resultat et heure de la derniere lecture
_sleep = time.sleep  # remplaces par les tests
_monotonic = time.monotonic


class _SingleTryFetcher(PoliteFetcher):
    """Une tentative par requete, sans intervalle ni ralentissement internes : les attentes du sitemap sont toutes
    decidees (et bornees) par SourceListingFetcher."""

    def __init__(self, cache_dir: str | Path, user_agent: str, base: PoliteFetcher | None = None):
        super().__init__(cache_dir, min_interval_s=0, user_agent=user_agent, retries=1)
        if base is not None:  # meme client HTTP (transport), memes regles robots.txt et meme crochet d'attente
            self._client, self.respect_robots, self._robots, self._sleep = (base._client, base.respect_robots,
                                                                            base._robots, base._sleep)

    def _on_rate_limited(self, url: str) -> None:
        return None


def recent_sitemap_failure(output_dir: str | None, max_age_s: float) -> bool:
    """La derniere lecture du sitemap (marqueur de l'etat) a-t-elle echoue il y a moins de `max_age_s` ?"""
    if not output_dir or max_age_s <= 0:
        return False
    try:
        data = json.loads((Path(output_dir) / STATUS_PATH).read_text(encoding="utf-8"))
        return data.get("status") in ("stale", "unavailable") and time.time() - float(data["at"]) < max_age_s
    except (OSError, ValueError, KeyError, TypeError):
        return False


def urlset_xml(urls: dict[str, str | None]) -> bytes:
    """Sitemap XML simple (urlset) : URL -> <lastmod> (None : sans date)."""
    body = "".join(f"<url><loc>{escape(u)}</loc>" + (f"<lastmod>{escape(d)}</lastmod>" if d else "") + "</url>"
                   for u, d in urls.items())
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>").encode()


@dataclass
class SitemapRead:
    """Resultat de la lecture du sitemap, une fois par processus.

    `status` : "fresh" (lu en entier maintenant), "stale" (incomplet, complete par des copies anciennes : cache HTTP
    d'un enfant ou instantane de l'etat), "unavailable" (rien), "not_requested" (pas de sitemap configure ou run
    pilote). Pages "sitemap" : lues maintenant, leurs dates sont sures ; pages "sitemap_stale" : decouverte
    seulement, leurs dates ne servent jamais a ne pas relire une page.
    """

    status: str
    pages: list[RawPage] = field(default_factory=list)


@component
class SourceListingFetcher:
    """Récupère toutes les pages de listing et les documents programme décrits dans `sources`.

    Le sitemap est lu une seule fois par instance (donc par run), au premier lot et avant les listings ; le resultat,
    echec compris, est repris par les lots suivants.
    """

    def __init__(self, sources: dict[str, Any], cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT, sitemap_retry_wait_s: float = 180,
                 sitemap_interval_s: float = 10, sitemap_attempts: int = 4,
                 sitemap_backoff_s: list[float] | None = None, sitemap_budget_s: float = 1200,
                 output_dir: str | None = None, patient: bool = True):
        """:param sitemap_retry_wait_s: attente avant la nouvelle lecture complete d'un sitemap incomplet.
        :param sitemap_interval_s: intervalle entre deux requetes du sitemap (index, enfants).
        :param sitemap_attempts: tentatives par page du sitemap sur HTTP 429 (en plus des reprises du fetcher).
        :param sitemap_backoff_s: attentes entre ces tentatives (defaut 60, 120, 240 s ; Retry-After si plus long).
        :param sitemap_budget_s: au-dela, plus d'attente entre tentatives : la lecture echoue vite.
        :param output_dir: dossier de l'etat ; instantane du sitemap ecrit apres une lecture complete, relu en secours.
        :param patient: False : une seule tentative par page et pas de nouvelle lecture (lecture echouee juste avant
            dans le meme job).
        """
        self.sources = sources
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.sitemap_retry_wait_s = sitemap_retry_wait_s
        self.sitemap_interval_s = sitemap_interval_s
        self.sitemap_attempts = sitemap_attempts
        self.sitemap_backoff_s = list(sitemap_backoff_s) if sitemap_backoff_s is not None else [60.0, 120.0, 240.0]
        self.sitemap_budget_s = sitemap_budget_s
        self.output_dir = output_dir
        self.patient = patient
        self._fetcher: PoliteFetcher | None = None
        self._sitemap_fetcher: Any = None
        self._sitemap: SitemapRead | None = None
        self._sitemap_requests = 0

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    def _targets(self, countries: list[str] | None) -> list[tuple[str, str, str, str | None]]:
        s = self.sources
        isos = countries or list(COUNTRIES)
        targets = [("factsheet", iso, s["factsheet_url"].format(slug=slug_from_iso(iso)), None) for iso in isos]
        targets.append(("spotlight", "spotlight", s["spotlight_url"], None))
        targets += [("policy_area", slug, s["policy_area_url"].format(slug=slug), None) for slug in s["policy_areas"]]
        if not countries:  # le PDF SRSP couvre tous les pays : exclu des runs pilotes
            targets.append(("srsp_pdf", "srsp", s["srsp_pdf_url"], None))
        targets += [("programme_doc", d["id"], d["url"], d.get("title")) for d in s.get("programme_docs", [])]
        return targets

    # --- sitemap -------------------------------------------------------------------------------------------------
    def _sitemap_client(self) -> Any:
        """Fetcher des requetes du sitemap : une seule tentative, sans attente interne (toutes les attentes sont ici,
        dans le budget) ; meme cache disque que le fetcher partage. Un fetcher injecte (tests) sert tel quel."""
        if self._sitemap_fetcher is None:
            if self._fetcher is not None and not isinstance(self._fetcher, PoliteFetcher):
                self._sitemap_fetcher = self._fetcher
            else:
                base = self._fetcher
                self._sitemap_fetcher = _SingleTryFetcher(
                    base.cache_dir if base is not None else self.cache_dir,
                    user_agent=base.user_agent if base is not None else self.user_agent, base=base)
        return self._sitemap_fetcher

    def _sitemap_get(self, url: str, deadline: float) -> bytes:
        """Requete du sitemap, espacee des precedentes (sauf memo) ; HTTP 429 : nouvelles tentatives patientes.

        Aucune requete apres `deadline` ; attente (palier ou Retry-After) bornee au budget restant."""
        client = self._sitemap_client()
        attempts = max(1, self.sitemap_attempts) if self.patient else 1
        for attempt in range(1, attempts + 1):
            if _monotonic() > deadline:
                raise FetchError(url, "sitemap time budget exhausted")
            memo = url in getattr(client, "_memo", {})
            pause = min(self.sitemap_interval_s, deadline - _monotonic())
            if self._sitemap_requests and pause > 0 and not memo:
                _sleep(pause)
            self._sitemap_requests += not memo
            try:
                return client.get(url).content
            except FetchError as exc:
                if exc.status != 429 or attempt >= attempts:
                    raise
                steps = self.sitemap_backoff_s or [0.0]
                wait = min(max(steps[min(attempt - 1, len(steps) - 1)], exc.retry_after or 0),
                           deadline - _monotonic())
                if wait <= 0:
                    raise
                runlog.event(NAME, "sitemap", url, "rate_limited", attempt=attempt, wait_s=wait)
                _sleep(wait)
        raise AssertionError("unreachable")

    def _read_sitemap(self, index_url: str, deadline: float) -> tuple[list[str], dict[str, RawPage], list[str]] | None:
        """(sitemaps enfants dans l'ordre de l'index, pages lues par URL, enfants en echec) ; None si l'index lui-meme
        est illisible."""
        try:
            content = self._sitemap_get(index_url, deadline)
            children, _ = parse_sitemap(content)
        except (FetchError, OSError, ValueError) as exc:
            runlog.event(NAME, "error", index_url, "sitemap_incomplete", error=repr(exc))
            return None
        if not children:  # sitemap simple, sans index
            return [index_url], {index_url: RawPage(kind="sitemap", key=index_url, url=index_url, content=content)}, []
        pages: dict[str, RawPage] = {}
        failed: list[str] = []
        for child in children:
            try:
                body = self._sitemap_get(child, deadline)
                parse_sitemap(body)
                pages[child] = RawPage(kind="sitemap", key=index_url, url=child, content=body)
            except (FetchError, OSError, ValueError) as exc:
                failed.append(child)
                runlog.event(NAME, "error", index_url, "sitemap_incomplete", child=child, error=repr(exc))
        return children, pages, failed

    def _cached_child(self, index_url: str, child: str) -> RawPage | None:
        """Copie en cache HTTP d'un sitemap enfant illisible (decouverte seulement) ; None si absente ou invalide."""
        try:
            content = self._get_fetcher().cached(child)
            if content is None:
                return None
            parse_sitemap(content)
        except (OSError, ValueError):
            return None
        runlog.event(NAME, "sitemap", child, "sitemap_child_cached")
        return RawPage(kind="sitemap_stale", key=index_url, url=child, content=content)

    def _snapshot_path(self) -> Path | None:
        return Path(self.output_dir) / SNAPSHOT_PATH if self.output_dir else None

    def _save_snapshot(self, index_url: str, pages: list[RawPage]) -> None:
        """Instantane de la lecture complete : URL -> <lastmod>, date de lecture (secours de la decouverte)."""
        path = self._snapshot_path()
        if path is None:
            return
        urls: dict[str, str | None] = {}
        for page in pages:
            dates = sitemap_lastmods(page.content)
            urls.update({u: dates.get(u) for u in parse_sitemap(page.content)[1]})
        data = {"index_url": index_url, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "urls": dict(sorted(urls.items()))}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:  # confort : le run continue sans instantane
            runlog.event(NAME, "error", index_url, "snapshot_write_failed", error=repr(exc))

    def _snapshot_page(self, index_url: str) -> RawPage | None:
        """Instantane de la derniere lecture complete (decouverte seulement) ; None s'il manque ou est illisible."""
        path = self._snapshot_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else None
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or data.get("index_url") != index_url or not data.get("urls"):
            return None
        runlog.event(NAME, "sitemap", index_url, "snapshot_used", fetched_at=data.get("fetched_at"),
                     urls=len(data["urls"]))
        return RawPage(kind="sitemap_stale", key=index_url, url=path.as_posix(), content=urlset_xml(data["urls"]))

    def read_sitemap(self) -> SitemapRead:
        """Sitemap configure, lu une fois par instance (resultat, echec compris, memorise).

        Lecture incomplete : nouvelle lecture complete apres `sitemap_retry_wait_s` (les pages deja lues viennent du
        memo du fetcher). Si elle reste incomplete : enfant illisible -> sa copie du cache HTTP ; sans copie, ou index
        illisible -> instantane de l'etat. Ces copies ne servent qu'a la decouverte (pages "sitemap_stale").
        """
        if self._sitemap is not None:
            return self._sitemap
        index_url = self.sources.get("sitemap_url")
        if not index_url:
            self._sitemap = SitemapRead("not_requested")
            return self._sitemap
        # Budget de toute la lecture (attentes et nouvelle lecture comprises) : pire cas ~ budget + durees des
        # requetes elles-memes.
        deadline = _monotonic() + self.sitemap_budget_s
        read = self._read_sitemap(index_url, deadline)
        if (read is None or read[2]) and self.patient:
            remaining = deadline - _monotonic()
            if remaining > self.sitemap_retry_wait_s:
                if self.sitemap_retry_wait_s > 0:
                    _sleep(self.sitemap_retry_wait_s)
                read = self._read_sitemap(index_url, deadline)
        if read is not None and not read[2]:
            order, pages, _ = read
            fresh = [pages[u] for u in order]
            self._save_snapshot(index_url, fresh)
            self._sitemap = SitemapRead("fresh", fresh)
        else:
            out: list[RawPage] = []
            missing = read is None
            if read is not None:
                order, pages, _ = read
                for child in order:
                    page = pages.get(child) or self._cached_child(index_url, child)
                    if page is None:
                        missing = True
                    else:
                        out.append(page)
            if missing:
                snapshot = self._snapshot_page(index_url)
                if snapshot is not None:
                    out.append(snapshot)
            # Enfants lus maintenant gardes (dates sures pour leurs pages) ; le sitemap reste incomplet.
            stale = any(p.kind == "sitemap_stale" for p in out)
            self._sitemap = SitemapRead("stale" if stale else "unavailable", out)
        runlog.event(NAME, "sitemap", index_url, self._sitemap.status, pages=len(self._sitemap.pages))
        self._save_status(self._sitemap.status)
        return self._sitemap

    def _save_status(self, status: str) -> None:
        """Marqueur de la derniere lecture (state/sitemap_status.json) : un etage suivant du meme job (deliverables)
        ne refait pas la lecture patiente si celle-ci vient d'echouer (recent_sitemap_failure)."""
        if not self.output_dir:
            return
        path = Path(self.output_dir) / STATUS_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"status": status, "at": time.time()}), encoding="utf-8")
        except OSError as exc:
            runlog.event(NAME, "error", str(path), "status_write_failed", error=repr(exc))

    def fetch_sitemap(self) -> list[RawPage]:
        """Pages du sitemap lues maintenant et en entier ; [] sinon (absent, indisponible, incomplet ou ancien) : les
        appelants s'en servent pour ne pas relire des pages, ce qu'une copie ancienne ne permet jamais."""
        read = self.read_sitemap()
        return list(read.pages) if read.status == "fresh" else []

    @component.output_types(pages=list[RawPage], sitemap_status=str)
    def run(self, countries: list[str] | None = None) -> dict[str, Any]:
        # Sitemap d'abord, une fois par run ; tous pays seulement (exclu des runs pilotes, comme le PDF).
        sitemap = self.read_sitemap() if not countries else SitemapRead("not_requested")
        pages = []
        for kind, key, url, title in self._targets(countries):
            try:
                result = self._get_fetcher().get(url)
            except FetchError as exc:
                runlog.event(NAME, "error", url, "fetch_failed", kind=kind, key=key, error=str(exc))
                continue
            except OSError as exc:
                runlog.event(NAME, "error", url, "fetch_failed", kind=kind, key=key, error=repr(exc))
                continue
            pages.append(RawPage(kind=kind, key=key, url=url, content=result.content, title=title))
        pages += [p.model_copy() for p in sitemap.pages]
        runlog.event(NAME, "discover", None, "ok", pages=len(pages), sitemap=sitemap.status)
        return {"pages": pages, "sitemap_status": sitemap.status}
