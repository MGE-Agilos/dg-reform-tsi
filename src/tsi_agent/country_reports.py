"""Country reports du Semestre europeen (depuis 2026) : un document de travail de la Commission (SWD) par Etat membre.

Chaque rapport (une centaine de pages, PDF anglais) analyse la situation economique et sociale du pays ; les
recommandations et leurs considerants en sont la conclusion. Source : la page pays d'ECFIN (PAGE_URL), dont le premier
lien de telechargement anglais du rapport de l'annee (nom de fichier « .._SWD_<annee>_... ») est le rapport.

`fetch_reports` telecharge les rapports absents (fetcher poli, extraction pdfplumber des livrables) : celui de
l'annee en cours s'il est publie, sinon celui de l'annee precedente (jamais avant YEAR) ; il ecrit
output/country_reports/<ISO>-<annee>.md : front matter (pays, annee, liens, pages) puis le texte, page par page
(marqueurs `--- page N ---`). Sans LLM.

`chunks` decoupe un rapport en passages cherchables (environ CHUNK_CHARS caracteres, coupes entre phrases, chacun
avec sa page de debut) : pieds de page et pages presque vides retires, tableaux de chiffres (peu de lettres) ecartes.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import yaml

from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.render import parse_front_matter

YEAR = 2026  # premiers rapports repris (les plus anciens ne sont pas telecharges)
DIR = "country_reports"
PAGE_URL = ("https://economy-finance.ec.europa.eu/economic-surveillance-eu-member-states/"
            "country-pages-including-country-reports/country-report-{slug}_en")
SITE = "https://economy-finance.ec.europa.eu"
CHUNK_CHARS = 1500
MIN_PAGE_CHARS = 200
MIN_LETTER_SHARE = 0.6  # passages de tableaux (chiffres, separateurs) ecartes en dessous

_HREF = re.compile(r'href="([^"]*/document/download/[^"]+)"')
_PAGE = re.compile(r"^--- page (\d+) ---[ \t]*$", re.MULTILINE)
_FOOTER = re.compile(r"^(?:EN|EN EN|\d{1,3})$")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(‘“])")


GUTTER_CROSSING = 0.01  # part des mots qu'une goutiere de colonnes peut traverser (titres pleine largeur)
GUTTER_MARGIN = 0.15  # pas de goutiere dans les 15 % du bord d'une zone
MIN_SIDE = 0.1  # chaque colonne garde au moins 10 % des mots de la zone


def _gutter(words: list[dict], x0: float, x1: float) -> float | None:
    """Milieu du plus large couloir vertical (presque) sans mot entre deux colonnes de la zone [x0, x1], ou None."""
    if len(words) < 20:
        return None
    limit = len(words) * GUTTER_CROSSING
    lo, hi = x0 + (x1 - x0) * GUTTER_MARGIN, x1 - (x1 - x0) * GUTTER_MARGIN
    free = [x for x in range(int(lo), int(hi) + 1)
            if sum(1 for w in words if w["x0"] < x < w["x1"]) <= limit]
    runs: list[list[int]] = []
    for x in free:
        if runs and x == runs[-1][-1] + 1:
            runs[-1].append(x)
        else:
            runs.append([x])
    for run in sorted(runs, key=len, reverse=True):
        if len(run) < 4:
            break
        mid = (run[0] + run[-1]) / 2
        left = sum(1 for w in words if w["x1"] <= mid)
        if min(left, len(words) - left) >= len(words) * MIN_SIDE:
            return mid
    return None


def column_text(page: Any, depth: int = 2) -> str:
    """Texte d'une page pdfplumber colonne par colonne (gauche puis droite, jusqu'a `depth` decoupes) : sur les pages
    a deux colonnes, extract_text melangerait les lignes des deux colonnes."""
    def region(bbox: tuple[float, float, float, float], level: int) -> list[str]:
        crop = page.within_bbox(bbox) if bbox != tuple(page.bbox) else page
        words = crop.extract_words()
        cut = _gutter(words, bbox[0], bbox[2]) if level < depth else None
        if cut is None:
            return [crop.extract_text() or ""]
        return region((bbox[0], bbox[1], cut, bbox[3]), level + 1) + region((cut, bbox[1], bbox[2], bbox[3]), level + 1)
    return "\n".join(t for t in region(tuple(page.bbox), 0) if t.strip())


def pdf_text_columns(data: bytes) -> tuple[int, str]:
    """(nombre de pages, texte) comme pdf_text des livrables, colonne par colonne."""
    import io

    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [f"--- page {i} ---\n{column_text(page)}" for i, page in enumerate(pdf.pages, start=1)]
    return len(pages), "\n\n".join(pages)


def page_url(country: str) -> str:
    return PAGE_URL.format(slug=name_from_iso(country).lower().replace(" ", "-"))


def find_pdf(page_html: str, year: int = YEAR) -> str | None:
    """Lien absolu du rapport anglais de l'annee : premier lien « ..._en?filename=..SWD_<annee>.. .pdf »."""
    for raw in _HREF.findall(page_html):
        href = html.unescape(raw)
        name = href.split("filename=", 1)[-1] if "filename=" in href else ""
        if "_en?" in href and name.lower().endswith(".pdf") and f"SWD_{year}" in name.upper() \
                and f"_{year - 1}_" not in name:
            return href if href.startswith("http") else SITE + href
    return None


@dataclass
class Report:
    country: str
    year: int
    source_url: str
    page_url: str
    pages: int
    text: str

    @property
    def report_id(self) -> str:
        return f"cr-{self.country}-{self.year}"


def report_path(output_dir: Path, country: str, year: int = YEAR) -> Path:
    return output_dir / DIR / f"{country}-{year}.md"


def write_report(output_dir: Path, report: Report) -> Path:
    path = report_path(output_dir, report.country, report.year)
    path.parent.mkdir(parents=True, exist_ok=True)
    front = {"report_id": report.report_id, "country": report.country, "year": report.year,
             "title": f"{name_from_iso(report.country)} — {report.year} country report",
             "source_url": report.source_url, "page_url": report.page_url, "pages": report.pages}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"---\n{yaml.safe_dump(front, allow_unicode=True, sort_keys=False)}---\n\n{report.text}\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def load_reports(output_dir: Path) -> list[Report]:
    """Rapport le plus recent de chaque pays (la page et l'assistant decrivent une annee par pays)."""
    latest: dict[str, Report] = {}
    for path in sorted((output_dir / DIR).glob("*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except ValueError:
            continue
        if isinstance(fm, dict) and fm.get("country") in COUNTRIES and isinstance(fm.get("year"), int):
            report = Report(fm["country"], fm["year"], str(fm.get("source_url") or ""), str(fm.get("page_url") or ""),
                            int(fm.get("pages") or 0), body)
            if fm["country"] not in latest or report.year > latest[fm["country"]].year:
                latest[fm["country"]] = report
    return [latest[c] for c in sorted(latest)]


def fetch_reports(output_dir: Path, fetcher: Any, countries: list[str] | None = None, year: int | None = None,
                  reader: Callable[[bytes], tuple[int, str]] | None = None, force: bool = False,
                  today: int | None = None) -> dict[str, Any]:
    """Telecharge les rapports absents (ou tous avec `force`) ; resume {fetched, skipped, failed: {pays: raison}}.

    Sans `year` : l'annee en cours si la page du pays publie son rapport, sinon l'annee precedente ; un rapport deja
    present pour l'une de ces annees est garde (skipped). Jamais avant YEAR.
    """
    from tsi_agent.http import FetchError

    reader = reader or pdf_text_columns
    last = today or date.today().year
    years = [year] if year else [y for y in (last, last - 1) if y >= YEAR]
    summary: dict[str, Any] = {"fetched": [], "skipped": [], "failed": {}}
    for country in countries or list(COUNTRIES):
        url = page_url(country)
        page_html: str | None = None
        outcome: str | None = None
        for y in years:
            if not force and report_path(output_dir, country, y).exists():
                outcome = "skipped"
                break
            try:
                if page_html is None:
                    page_html = fetcher.get(url).content.decode("utf-8", "replace")
                pdf = find_pdf(page_html, y)
                if not pdf:
                    continue
                data = fetcher.get(pdf, cache=False).content
                if not data.startswith(b"%PDF"):
                    summary["failed"][country] = "download is not a PDF"
                    outcome = "failed"
                    break
                pages, text = reader(data)
            except FetchError as exc:
                summary["failed"][country] = str(exc)
                outcome = "failed"
                break
            except Exception as exc:  # pdfplumber leve des types varies sur un fichier abime
                summary["failed"][country] = f"{type(exc).__name__}: {exc}"
                outcome = "failed"
                break
            write_report(output_dir, Report(country, y, pdf, url, pages, text))
            outcome = "fetched"
            break
        if outcome is None:
            summary["failed"][country] = f"no {' or '.join(map(str, years))} English report link on {url}"
        elif outcome != "failed":
            summary[outcome].append(country)
    return summary


@dataclass
class Chunk:
    country: str
    year: int
    page: int
    seq: int
    text: str
    source_url: str

    @property
    def chunk_id(self) -> str:
        return f"cr-{self.country}-{self.year}-p{self.page}-{self.seq}"


def _page_text(page: str) -> str:
    """Texte d'une page en un bloc : pied de page retire, lignes recollees (trait d'union de fin de ligne garde)."""
    lines = [line.strip() for line in page.split("\n")]
    while lines and (not lines[-1] or _FOOTER.match(lines[-1])):
        lines.pop()
    text = ""
    for line in lines:
        if not line:
            continue
        text = text + line if text.endswith("-") else (f"{text} {line}" if text else line)
    return " ".join(text.split())


def _letter_share(text: str) -> float:
    visible = [c for c in text if not c.isspace()]
    return sum(c.isalpha() for c in visible) / len(visible) if visible else 0.0


def chunks(report: Report, size: int = CHUNK_CHARS) -> list[Chunk]:
    """Passages de `size` caracteres environ, coupes entre phrases, chacun avec sa page de debut."""
    parts = _PAGE.split(report.text)
    out: list[Chunk] = []
    for i in range(1, len(parts), 2):
        page, text = int(parts[i]), _page_text(parts[i + 1])
        if len(text) < MIN_PAGE_CHARS:
            continue
        current, seq = "", 0
        sentences = _SENTENCE_END.split(text)
        for sentence in sentences:
            if current and len(current) + len(sentence) + 1 > size:
                seq += 1
                out.append(Chunk(report.country, report.year, page, seq, current, report.source_url))
                current = sentence
            else:
                current = f"{current} {sentence}" if current else sentence
        if current:
            seq += 1
            out.append(Chunk(report.country, report.year, page, seq, current, report.source_url))
    return [c for c in out if _letter_share(c.text) >= MIN_LETTER_SHARE]
