"""Parser d'une page détail projet."""

import re
from dataclasses import dataclass

from tsi_agent.ids import clean_text
from tsi_agent.parsers import main_of, main_text, soup_of


@dataclass
class DetailParse:
    title: str
    text: str
    programme: str | None
    year: int | None


def _programme(value: str) -> str | None:
    if "Technical Support Instrument" in value or "(TSI)" in value:
        return "TSI"
    if "Structural Reform Support" in value or "SRSP" in value:
        return "SRSP"
    return None


def parse_detail(html: bytes | str) -> DetailParse:
    soup = soup_of(html)
    h1 = soup.find("h1")
    meta: dict[str, str] = {}
    for dt in main_of(soup).select("dl.ecl-description-list dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            meta[clean_text(dt.get_text(" "))] = clean_text(dd.get_text(" "))
    year_match = re.search(r"\d{4}", meta.get("Year", ""))
    return DetailParse(
        title=clean_text(h1.get_text(" ")) if h1 else "",
        text=main_text(html),
        programme=_programme(meta.get("Funding Programme", "")),
        year=int(year_match.group(0)) if year_match else None,
    )
