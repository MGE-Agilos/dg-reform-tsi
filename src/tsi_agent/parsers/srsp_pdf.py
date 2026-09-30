"""Parser du PDF « List of approved technical support requests under the SRSP »."""

import io
import re

import pdfplumber

from tsi_agent.countries import iso_from_name
from tsi_agent.ids import clean_text, title_key
from tsi_agent.models import ProjectRef

_FUNDING = re.compile(r"^(?:SRSP)?(\d{4})$")
_HEADER_CELLS = {"Funding", "source", "Title of the request", "Title of the project"}


def _country_headings(page) -> list[tuple[float, str]]:
    """Titres de pays en gras, avec leur position verticale."""
    lines: dict[int, list[str]] = {}
    for word in page.extract_words(extra_attrs=["fontname"]):
        if "Bold" in word["fontname"]:
            lines.setdefault(round(word["top"]), []).append(word["text"])
    found = []
    for top, words in sorted(lines.items()):
        iso = iso_from_name(" ".join(words))
        if iso:
            found.append((top, iso))
    return found


def _is_continuation(row: list[str | None]) -> bool:
    """Suite d'un titre sur plusieurs lignes : une seule cellule remplie, toutes les autres à None."""
    filled = [c for c in row if c]
    return row[0] is None and len(filled) == 1 and sum(c is None for c in row) == len(row) - 1


def parse_srsp_rows(pdf_bytes: bytes) -> list[tuple[str, int, str]]:
    """Retourne des tuples (pays ISO, année, titre) dans l'ordre du document."""
    rows: list[tuple[str, int, str]] = []
    country: str | None = None
    year: int | None = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            headings = _country_headings(page)
            for table in sorted(page.find_tables(), key=lambda t: t.bbox[1]):
                above = [iso for top, iso in headings if top < table.bbox[1]]
                if above and above[-1] != country:
                    country, year = above[-1], None
                for row in table.extract():
                    if _is_continuation(row) and rows:
                        c, y, t = rows[-1]
                        rows[-1] = (c, y, clean_text(t + " " + next(x for x in row if x)))
                        continue
                    cells = [clean_text(c) for c in row if c and clean_text(c)]
                    if not cells or any(c in _HEADER_CELLS for c in cells):
                        continue
                    m = _FUNDING.match(cells[0])
                    rest = cells[1:] if m else cells
                    if m:
                        year = int(m.group(1))
                    if rest and country and year:
                        rows.append((country, year, clean_text(" ".join(rest))))
    return rows


def parse_srsp_pdf(pdf_bytes: bytes, url: str) -> list[ProjectRef]:
    return [
        ProjectRef(project_id=title_key("SRSP", year, title), title=title, programme="SRSP", year=year,
                   countries=[iso], listing_urls=[url])
        for iso, year, title in parse_srsp_rows(pdf_bytes)
    ]
