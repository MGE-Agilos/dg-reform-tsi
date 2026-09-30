"""Parser du PDF « List of approved technical support requests under the SRSP »."""

import io
import re

import pdfplumber

from tsi_agent import runlog
from tsi_agent.countries import iso_from_name
from tsi_agent.ids import clean_text, title_key
from tsi_agent.models import ProjectRef

_FUNDING = re.compile(r"^(?:SRSP)?(\d{4})$")
_HEADER_CELLS = {"Funding", "source", "Title of the request", "Title of the project"}
_COUNTRY_LIKE = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+){0,3}$")


def heading_kind(text: str) -> str | None:
    """Code ISO si text est un nom de pays connu, "unknown" si ça y ressemble sans l'être, sinon None."""
    iso = iso_from_name(text)
    if iso:
        return iso
    if text in _HEADER_CELLS:
        return None
    if _COUNTRY_LIKE.match(text):
        return "unknown"
    return None


def _country_headings(page) -> list[tuple[float, str, str]]:
    """Titres de pays (connus ou non) en gras, avec leur position verticale et leur texte."""
    lines: dict[int, list[str]] = {}
    for word in page.extract_words(extra_attrs=["fontname"]):
        if "Bold" in word["fontname"]:
            lines.setdefault(round(word["top"]), []).append(word["text"])
    found = []
    for top, words in sorted(lines.items()):
        text = " ".join(words)
        kind = heading_kind(text)
        if kind:
            found.append((top, kind, text))
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
    alerted: set[str] = set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            headings = _country_headings(page)
            for table in sorted(page.find_tables(), key=lambda t: t.bbox[1]):
                above = [(kind, text) for top, kind, text in headings if top < table.bbox[1]]
                if above:
                    kind, text = above[-1]
                    if kind == "unknown":
                        if text not in alerted:
                            alerted.add(text)
                            runlog.event("SrspPdfParser", "alert", text, "warning",
                                         message=f"SRSP PDF: unknown country heading '{text}', rows skipped")
                        country, year = None, None
                    elif kind != country:
                        country, year = kind, None
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
