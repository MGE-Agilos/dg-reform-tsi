"""Parser d'une page détail projet."""

import re
from dataclasses import dataclass, field

from bs4 import PageElement, Tag

from tsi_agent.ids import clean_text
from tsi_agent.parsers import blocks_markdown, main_of, main_text, soup_of

# Titres h3 de mise en page, sans contenu de projet (comparaison sans casse).
BOILERPLATE_HEADINGS = {"more about the project", "useful links", "photos visuals", "photos", "videos"}
_ACHIEVED = re.compile(r"\bresults? achieved\b", re.IGNORECASE)
_EXPECTED = re.compile(r"\bexpected results?\b|\bresults expected\b", re.IGNORECASE)


@dataclass
class DetailParse:
    title: str
    text: str  # texte brut de <main> : base du content_hash, ne pas modifier
    programme: str | None
    year: int | None
    intro: str = ""  # paragraphe d'accroche officiel (entre le titre h2 et le premier h3), en Markdown
    sections: list[tuple[str, str]] = field(default_factory=list)  # (titre h3 tel quel, corps Markdown)
    results: str | None = None  # "achieved" | "expected" | None


def _programme(value: str) -> str | None:
    if "Technical Support Instrument" in value or "(TSI)" in value:
        return "TSI"
    if "Structural Reform Support" in value or "SRSP" in value:
        return "SRSP"
    return None


def _is_boundary(node: PageElement) -> bool:
    """Un h2/h3 (ou un bloc qui en contient) ferme la section en cours."""
    return isinstance(node, Tag) and (node.name in ("h2", "h3") or node.find(["h2", "h3"]) is not None)


def _until_next_heading(start: Tag) -> list[PageElement]:
    nodes = []
    for node in start.next_siblings:
        if _is_boundary(node):
            break
        nodes.append(node)
    return nodes


def _results_kind(headings: list[str]) -> str | None:
    for heading in headings:
        if _ACHIEVED.search(heading):
            return "achieved"
        if _EXPECTED.search(heading):
            return "expected"
    return None


def parse_detail(html: bytes | str) -> DetailParse:
    soup = soup_of(html)
    main = main_of(soup)
    h1 = soup.find("h1")
    meta: dict[str, str] = {}
    for dt in main.select("dl.ecl-description-list dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            meta[clean_text(dt.get_text(" "))] = clean_text(dd.get_text(" "))
    year_match = re.search(r"\d{4}", meta.get("Year", ""))
    title_h2 = main.find("h2")
    intro = blocks_markdown(_until_next_heading(title_h2)) if title_h2 is not None else ""
    sections = []
    for h3 in main.find_all("h3"):
        heading = clean_text(h3.get_text(" "))
        if not heading or heading.casefold() in BOILERPLATE_HEADINGS:
            continue
        sections.append((heading, blocks_markdown(_until_next_heading(h3))))
    return DetailParse(
        title=clean_text(h1.get_text(" ")) if h1 else "",
        text=main_text(html),
        programme=_programme(meta.get("Funding Programme", "")),
        year=int(year_match.group(0)) if year_match else None,
        intro=intro,
        sections=sections,
        results=_results_kind([h for h, _ in sections]),
    )
