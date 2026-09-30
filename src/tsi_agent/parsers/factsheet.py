"""Parser d'une TSI Country Factsheet (un pays)."""

import copy
import re
from dataclasses import dataclass, field

from tsi_agent.countries import name_from_iso
from tsi_agent.ids import clean_text, slug_from_url, title_key
from tsi_agent.models import ProjectRef
from tsi_agent.parsers import main_of, soup_of
from tsi_agent.urls import absolutize, is_detail_candidate

_YEAR = re.compile(r"^\d{4}$")
_YEAR_PREFIX = re.compile(r"^(\d{4})\s*[-–—]\s*(.+)$")


@dataclass
class FactsheetParse:
    refs: list[ProjectRef] = field(default_factory=list)
    announced: dict[int, int | None] = field(default_factory=dict)
    parsed_counts: dict[int, int] = field(default_factory=dict)
    programme_text: str = ""


def _detail_url(li) -> str | None:
    a = li.find("a", href=True)
    if a is None:
        return None
    url = absolutize(a["href"])
    return url if is_detail_candidate(url) else None


def _has_li_ancestor(el, content) -> bool:
    """Vrai si un ancêtre de el (avant d'atteindre content) est un <li>."""
    parent = el.parent
    while parent is not None and parent is not content:
        if parent.name == "li":
            return True
        parent = parent.parent
    return False


def _own_text(el) -> str:
    """Texte de el sans ses listes imbriquées (ul/ol), sur une copie détachée."""
    clone = copy.copy(el)
    for nested in clone.find_all(["ul", "ol"]):
        nested.decompose()
    return clean_text(clone.get_text(" "))


def parse_factsheet(html: bytes | str, url: str, country_iso: str) -> FactsheetParse:
    soup = soup_of(html)
    main = main_of(soup)
    out = FactsheetParse()

    for item in soup.select("details.ecl-accordion__item"):
        summary = item.find("summary")
        year_text = clean_text(summary.get_text()) if summary else ""
        if not _YEAR.match(year_text):
            continue
        year = int(year_text)
        content = item.select_one(".ecl-accordion__content") or item
        strong = content.find("strong")
        strong_text = clean_text(strong.get_text()) if strong else ""
        out.announced[year] = int(strong_text) if strong_text.isdigit() else None
        multi = False
        count = 0
        for el in content.find_all(["h3", "h4", "h5", "p", "li"]):
            if _has_li_ancestor(el, content):
                continue
            if el.name != "li":
                text = clean_text(el.get_text(" "))
                if text.lower().startswith("multi-country"):
                    multi = True
                continue
            text = _own_text(el)
            if not text:
                continue
            count += 1
            thin_id = title_key("TSI", year, text)
            detail = _detail_url(el)
            out.refs.append(ProjectRef(
                project_id=slug_from_url(detail) if detail else thin_id,
                title=text, programme="TSI", year=year, countries=[country_iso], multi_country=multi,
                detail_url=detail, listing_urls=[url], aliases=[thin_id] if detail else [],
            ))
        out.parsed_counts[year] = count

    highlights = []
    for card in main.select("article.ecl-card"):
        a = card.find("a", href=True)
        if a is None:
            continue
        detail = absolutize(a["href"])
        if not is_detail_candidate(detail):
            continue
        raw_title = clean_text(a.get_text(" "))
        m = _YEAR_PREFIX.match(raw_title)
        year, title = (int(m.group(1)), m.group(2)) if m else (None, raw_title)
        desc = card.select_one(".ecl-content-block__description")
        highlights.append(f"- {raw_title}: {clean_text(desc.get_text(' ')) if desc else ''}".rstrip(": "))
        out.refs.append(ProjectRef(
            project_id=slug_from_url(detail), title=title, year=year, countries=[country_iso],
            detail_url=detail, highlighted=True, listing_urls=[url],
        ))

    facts = [clean_text(f.get_text(" ")) for f in main.select(".ecl-fact-figures__item")]
    name = name_from_iso(country_iso)
    parts = [f"TSI country factsheet - {name}", "", "Completed and ongoing reform projects:"]
    parts += [f"- {f}" for f in facts]
    if highlights:
        parts += ["", "Highlighted projects:", *highlights]
    out.programme_text = "\n".join(parts)
    return out
