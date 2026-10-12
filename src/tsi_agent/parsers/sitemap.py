"""Parser du sitemap XML du site (index de sitemaps ou liste d'URLs) et filtre des pages projet TSI."""

import re
from xml.etree import ElementTree

# Rubriques du site qui ne sont pas des pages projet : pages de navigation, partenaires, appels "flagship"
# (description d'un appel a projets : ni programme ni annee, pas de sections Context/Support delivered).
DEFAULT_EXCLUDED_AREAS = ("our-projects", "our-projects-0", "what-we-do", "other-eu-support-opportunities",
                          "our-partners", "flagship-technical-support-projects")
_PROJECT_URL = re.compile(
    r"^https://reforms-investments\.ec\.europa\.eu/technical-support-instrument-0/([a-z0-9-]+)/[a-z0-9-]+_en$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(xml: bytes | str) -> tuple[list[str], list[str]]:
    """(sitemaps enfants, URLs de pages) ; ValueError si le contenu n'est pas un sitemap XML."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"sitemap illisible : {exc}") from exc
    kind = _local(root.tag)
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError(f"sitemap inattendu : <{kind}>")
    locs = []
    for entry in root:
        loc = next((c for c in entry if _local(c.tag) == "loc"), None)
        if loc is not None and loc.text and loc.text.strip():
            locs.append(loc.text.strip())
    return (locs, []) if kind == "sitemapindex" else ([], locs)


def sitemap_project_urls(urls: list[str], excluded_areas: list[str] | tuple[str, ...]) -> list[str]:
    """URLs de la forme .../technical-support-instrument-0/<domaine>/<slug>_en hors domaines exclus, sans doublon."""
    excluded = set(excluded_areas)
    out: dict[str, None] = {}
    for url in urls:
        m = _PROJECT_URL.match(url)
        if m and m.group(1) not in excluded:
            out.setdefault(url, None)
    return list(out)
