"""Parser du sitemap XML du site (index de sitemaps ou liste d'URLs) et filtre des pages projet TSI."""

import re
from datetime import datetime, timezone
from xml.etree import ElementTree

# Rubriques du site qui ne sont pas des pages projet : pages de navigation, partenaires, appels "flagship"
# (description d'un appel a projets : ni programme ni annee, pas de sections Context/Support delivered).
DEFAULT_EXCLUDED_AREAS = ("our-projects", "our-projects-0", "what-we-do", "other-eu-support-opportunities",
                          "our-partners", "flagship-technical-support-projects")
_PROJECT_URL = re.compile(
    r"^https://reforms-investments\.ec\.europa\.eu/technical-support-instrument-0/([a-z0-9-]+)/[a-z0-9-]+_en$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _entries(xml: bytes | str) -> tuple[str, list[tuple[str, str | None]]]:
    """(type de racine, [(loc, lastmod ou None)]) ; ValueError si le contenu n'est pas un sitemap XML."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"sitemap illisible : {exc}") from exc
    kind = _local(root.tag)
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError(f"sitemap inattendu : <{kind}>")
    out = []
    for entry in root:
        fields = {_local(c.tag): (c.text or "").strip() for c in entry if _local(c.tag) in ("loc", "lastmod")}
        if fields.get("loc"):
            out.append((fields["loc"], fields.get("lastmod") or None))
    return kind, out


def parse_sitemap(xml: bytes | str) -> tuple[list[str], list[str]]:
    """(sitemaps enfants, URLs de pages) ; ValueError si le contenu n'est pas un sitemap XML."""
    kind, entries = _entries(xml)
    locs = [loc for loc, _ in entries]
    return (locs, []) if kind == "sitemapindex" else ([], locs)


def sitemap_lastmods(xml: bytes | str) -> dict[str, str]:
    """URL de page -> date <lastmod> telle quelle (pages sans date omises ; {} pour un index de sitemaps)."""
    kind, entries = _entries(xml)
    return {} if kind == "sitemapindex" else {loc: lastmod for loc, lastmod in entries if lastmod}


def _instant(value: str | None) -> datetime | None:
    """Date W3C du sitemap (ex. 2025-12-01T15:25Z) -> instant UTC ; None si absente ou illisible."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def not_modified_since(current: str | None, recorded: str | None) -> bool:
    """La page n'a-t-elle pas change depuis sa date enregistree ? Faux si l'une des dates manque ou est illisible."""
    now, before = _instant(current), _instant(recorded)
    return now is not None and before is not None and now <= before


def sitemap_project_urls(urls: list[str], excluded_areas: list[str] | tuple[str, ...]) -> list[str]:
    """URLs de la forme .../technical-support-instrument-0/<domaine>/<slug>_en hors domaines exclus, sans doublon."""
    excluded = set(excluded_areas)
    out: dict[str, None] = {}
    for url in urls:
        m = _PROJECT_URL.match(url)
        if m and m.group(1) not in excluded:
            out.setdefault(url, None)
    return list(out)
