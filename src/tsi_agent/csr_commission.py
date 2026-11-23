"""CSR de l'annee en cours proposees par la Commission (2026), analysees sans LLM depuis le texte des 27 PDF.

La base ECFIN s'arrete aux CSR adoptees (2025). Les recommandations 2026 viennent de la publication
PUBLICATION_URL : un PDF anglais par Etat membre. Son texte integral (un `## <document>` par PDF, ligne
`Source: <url>`, marqueurs `--- page N ---`) a ete extrait par l'etage livrables ; il est lu dans
output/csr/publications/<id>.md (copie gardee par l'exclusion des livrables), a defaut output/deliverables/<id>.md.

Analyse deterministe : pays du titre du document (a defaut de la phrase « HEREBY RECOMMENDS that <pays> » ou du
prefixe ISO du nom de fichier), puis la partie entre « HEREBY RECOMMENDS that ... take action in ... to: » et
« Done at Brussels ». Les paragraphes numerotes 1., 2.... (numerotation suivie) deviennent les recommandations
csr-<ISO>-<annee>-<n>, sans sous-partie. Pieds de page (« EN EN », numero de page) et notes de bas de page retires.

Texte absent pour des pays : `fetch_missing` telecharge leurs PDF anglais depuis la page de publication (fetcher
poli, extraction pdfplumber des livrables) et `merge_texts` les ajoute a la copie locale.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsi_agent.countries import COUNTRIES, iso_from_name, name_from_iso
from tsi_agent.csr import COMMISSION_STAGE, Csr, Part, build_csrs, read_rows

YEAR = 2026
PUBLICATION_ID = "2026-european-semester-country-specific-recommendations-commission-recommendations"
PUBLICATION_URL = f"https://reforms-investments.ec.europa.eu/publications-0/{PUBLICATION_ID}_en"
PUBLICATIONS_DIR = "publications"  # sous-dossier de output/csr : non indexe (glob *.md non recursif)

_DOCUMENT = re.compile(r"^## (.+?)[ \t]*\n\nSource: (.*?)[ \t]*$", re.MULTILINE)
_PAGE = re.compile(r"^--- page \d+ ---[ \t]*$", re.MULTILINE)
_RECOMMENDS = re.compile(r"HEREBY RECOMMENDS that (?:the )?(.+?) take action in [^\n]*?to:[ \t]*$", re.MULTILINE)
_END = re.compile(r"^Done at Brussels", re.MULTILINE)
_FOOTER = re.compile(r"^(?:EN EN|EN|\d{1,3})$")
_NUMBERED = re.compile(r"^(\d{1,2})\.\s+([A-Z].*)$")  # une recommandation commence par une majuscule
_FOOTNOTE = re.compile(r"^(\d{1,3}) [A-Z(]")
_REF = re.compile(r"\((\d{1,3})\)")
_FILENAME_ISO = re.compile(r"filename=([A-Za-z]{2})-", re.IGNORECASE)


@dataclass
class DocumentParse:
    """Resultat de l'analyse d'un PDF : pays, lien, nombre de recommandations et probleme eventuel."""
    document: str
    url: str
    country: str | None
    n_recommendations: int = 0
    problem: str | None = None


@dataclass
class CommissionParse:
    csrs: list[Csr] = field(default_factory=list)
    documents: list[DocumentParse] = field(default_factory=list)
    recitals: list[Recital] = field(default_factory=list)

    @property
    def missing_countries(self) -> list[str]:
        """Etats membres sans aucune recommandation trouvee (texte absent ou illisible)."""
        found = {d.country for d in self.documents if d.country and d.n_recommendations}
        return sorted(c for c in COUNTRIES if c not in found)

    def report(self) -> dict[str, Any]:
        return {"documents": len(self.documents), "recommendations": len(self.csrs), "recitals": len(self.recitals),
                "by_country": {d.country or d.document: d.n_recommendations for d in self.documents},
                "alerts": [f"{d.country or d.document}: {d.problem}" for d in self.documents if d.problem],
                "missing_countries": self.missing_countries}


def document_country(document: str, url: str, text: str) -> str | None:
    """ISO du document : titre (« ... - Croatia »), puis phrase HEREBY RECOMMENDS, puis prefixe du nom de fichier."""
    tail = document.rsplit(" - ", 1)[-1].strip()
    iso = iso_from_name(tail)
    if iso:
        return iso
    match = _RECOMMENDS.search(text)
    if match and iso_from_name(match.group(1)):
        return iso_from_name(match.group(1))
    match = _FILENAME_ISO.search(url)
    if match:
        code = match.group(1).upper()
        code = "GR" if code == "EL" else code
        return code if code in COUNTRIES else None
    return None


def _clean_page(page: str, refs: set[str], numbered: re.Pattern[str] = _NUMBERED) -> list[str]:
    """Lignes d'une page sans pied de page (« EN EN », numero) ni notes de bas de page finales.

    Note de bas de page : bloc final qui commence par « <n> <Majuscule> », n appele « (n) » dans le document, sans
    paragraphe numerote (`numbered`) apres lui sur la page."""
    lines = [line.strip() for line in page.split("\n")]
    while lines and (not lines[-1] or _FOOTER.match(lines[-1])):
        lines.pop()
    for i, line in enumerate(lines):
        note = _FOOTNOTE.match(line)
        if note and note.group(1) in refs and not any(numbered.match(x) for x in lines[i:]):
            lines = lines[:i]
            break
    return [line for line in lines if line]


# Considerants (recitals) : paragraphes « (1) ... », « (2) ... » entre « Whereas: » et « HEREBY RECOMMENDS ». Ils
# portent l'analyse de la Commission (defis, contexte) dont les recommandations sont la conclusion.
_WHEREAS = re.compile(r"^Whereas:[ \t]*$", re.MULTILINE)
_RECITAL = re.compile(r"^\((\d{1,3})\)\s+([A-Z0-9‘“'\"].*)$")


@dataclass
class Recital:
    country: str
    year: int
    number: int
    text: str
    source_url: str

    @property
    def recital_id(self) -> str:
        return f"rec-{self.country}-{self.year}-{self.number}"


_PAREN_NOTE = re.compile(r"^\((\d{1,3})\)\s")
_BODY_WIDTH = 95  # caracteres : les notes (petite police) font des lignes plus longues que le corps du texte
_LOOSE_NOTE = re.compile(r"^(\d{1,3}) \S")


def _drop_note_area(lines: list[str], refs: set[str]) -> list[str]:
    """Zone des notes en bas de page restee apres _clean_page : suite d'une note commencee page precedente (ligne plus
    large que le corps, sans numero) ou note « n <texte> » qui ne commence pas par une majuscule (« 17 27% ... »,
    « 15 ‘Businesses... »). Coupe a la premiere de ces lignes si aucun considerant ne la suit sur la page."""
    for i, line in enumerate(lines):
        if _RECITAL.match(line) or any(_RECITAL.match(x) for x in lines[i + 1:]):
            continue
        note = _LOOSE_NOTE.match(line)
        if len(line) > _BODY_WIDTH or (note and note.group(1) in refs):
            return lines[:i]
    return lines


def _drop_paren_notes(lines: list[str], seen: int) -> tuple[list[str], int]:
    """Notes de bas de page numerotees « (n) ... » (certains documents, comme l'Irlande) : bloc final de la page dont
    la premiere ligne « (k) » n'est pas le considerant attendu, est appelee plus haut dans la meme page
    (« ... Report (k) on ... ») et dont les lignes « (m) » suivent la suite k, k+1...

    `seen` : considerants deja ouverts avant la page ; renvoie les lignes gardees et le nouveau compte."""
    for i, line in enumerate(lines):
        note = _RECITAL.match(line)
        if note and int(note.group(1)) == seen + 1:
            seen += 1
            continue
        note = _PAREN_NOTE.match(line)
        if not note or not i:
            continue
        k = int(note.group(1))
        if f"({k})" not in " ".join(lines[:i]):
            continue
        nums = [int(m.group(1)) for x in lines[i:] if (m := _PAREN_NOTE.match(x))]
        if nums == list(range(k, k + len(nums))):
            return lines[:i], seen
    return lines, seen


def recitals_text(text: str) -> str | None:
    """Partie « Whereas: » -> « HEREBY RECOMMENDS », pieds de page et notes retires ; None si absente."""
    start, end = _WHEREAS.search(text), _RECOMMENDS.search(text)
    if not start or not end or end.start() <= start.end():
        return None
    rest = text[start.end():end.start()]
    refs = set(_REF.findall(text))
    paren_notes = not re.search(r"^\d{1,3} [A-Z]", text, re.MULTILINE)  # aucune note « n Texte » : notes « (n) »
    pages = [_clean_page(page, refs, _RECITAL) for page in _PAGE.split(rest)]
    if paren_notes:  # d'abord les notes « (n) », qui ressemblent a des considerants
        seen, kept = 0, []
        for lines in pages:
            lines, seen = _drop_paren_notes(lines, seen)
            kept.append(lines)
        pages = kept
    pages = [_drop_note_area(lines, refs) for lines in pages]
    return "\n".join(line for lines in pages for line in lines)


def split_recitals(section: str) -> list[tuple[int, str]]:
    """(numero, texte) des considerants (1), (2)... dans l'ordre ; un « (n) » hors suite (renvoi a une note ou a une
    recommandation en debut de ligne) reste du texte."""
    out: list[tuple[int, list[str]]] = []
    for line in section.split("\n"):
        match = _RECITAL.match(line)
        if match and int(match.group(1)) == len(out) + 1:
            out.append((len(out) + 1, [match.group(2)]))
        elif out:
            out[-1][1].append(line)
    return [(n, _join(lines)) for n, lines in out]


def recommendations_text(text: str) -> str | None:
    """Partie « HEREBY RECOMMENDS ... to: » -> « Done at Brussels », pieds de page et notes retires ; None si absente."""
    start = _RECOMMENDS.search(text)
    if not start:
        return None
    rest = text[start.end():]
    end = _END.search(rest)
    rest = rest[:end.start()] if end else rest
    refs = set(_REF.findall(text))
    lines = [line for page in _PAGE.split(rest) for line in _clean_page(page, refs)]
    return "\n".join(lines)


def split_recommendations(section: str) -> list[tuple[str, str]]:
    """(numero, texte) des paragraphes 1., 2.... dans l'ordre ; une ligne « n. » hors suite, ou suivie d'une
    minuscule (« 4. per cent »), reste du texte."""
    out: list[tuple[str, list[str]]] = []
    for line in section.split("\n"):
        match = _NUMBERED.match(line)
        if match and int(match.group(1)) == len(out) + 1:
            out.append((match.group(1), [match.group(2)]))
        elif out:
            out[-1][1].append(line)
    return [(n, _join(lines)) for n, lines in out]


def _join(lines: list[str]) -> str:
    """Lignes d'un paragraphe PDF en une phrase, espaces normalises ; un trait d'union en fin de ligne est garde et
    recolle (« liquidation-related » : les textes de la Commission ne coupent pas les mots)."""
    text = ""
    for line in lines:
        if text.endswith("-"):
            text += line
        else:
            text = f"{text} {line}" if text else line
    return " ".join(text.split())


def parse_text(text: str, year: int = YEAR) -> CommissionParse:
    """Recommandations de chaque document de la publication (texte integral au format de l'etage livrables)."""
    result = CommissionParse()
    parts = _DOCUMENT.split(text.replace("\r\n", "\n"))
    seen: set[str] = set()
    for i in range(1, len(parts), 3):
        document, url, body = parts[i].strip(), parts[i + 1].strip(), parts[i + 2]
        country = document_country(document, url, body)
        doc = DocumentParse(document, url, country)
        result.documents.append(doc)
        if not country:
            doc.problem = "country not recognised"
            continue
        if country in seen:
            doc.problem = "duplicate document for this country (ignored)"
            continue
        section = recommendations_text(body)
        if section is None:
            doc.problem = "no 'HEREBY RECOMMENDS ... to:' part"
            continue
        recs = split_recommendations(section)
        if not recs:
            doc.problem = "0 recommendations found"
            continue
        seen.add(country)
        doc.n_recommendations = len(recs)
        for number, rec_text in recs:
            part = Part(f"csr-{country}-{year}-{number}", rec_text, [], [])
            result.csrs.append(Csr(country, year, number, part, [], stage=COMMISSION_STAGE, source_url=url))
        whereas = recitals_text(body)
        result.recitals.extend(Recital(country, year, n, t, url) for n, t in split_recitals(whereas or ""))
    return result


def source_paths(output_dir: Path) -> list[Path]:
    """Emplacements du texte integral, par priorite : copie de output/csr, puis texte de l'etage livrables."""
    return [output_dir / "csr" / PUBLICATIONS_DIR / f"{PUBLICATION_ID}.md",
            output_dir / "deliverables" / f"{PUBLICATION_ID}.md"]


def find_source(output_dir: Path) -> Path | None:
    return next((p for p in source_paths(output_dir) if p.exists()), None)


def load(output_dir: Path, year: int = YEAR) -> CommissionParse:
    """Analyse du texte local (vide s'il n'existe pas)."""
    path = find_source(output_dir)
    return parse_text(path.read_text(encoding="utf-8"), year) if path else CommissionParse()


def with_commission(csrs: list[Csr], commission: list[Csr]) -> list[Csr]:
    """CSR de la base ECFIN suivies des recommandations de la Commission d'une annee absente de la base (jamais en
    double : une fois les CSR adoptees publiees par ECFIN, la base l'emporte)."""
    known = {c.year for c in csrs}
    return [*csrs, *(c for c in commission if c.year not in known)]


def load_all_csrs(output_dir: Path, include_commission: bool = True) -> list[Csr]:
    """CSR de la base ECFIN (export xlsx, s'il existe) et, sauf demande contraire, recommandations de la Commission."""
    xlsx = output_dir / "csr" / "csr_full.xlsx"
    csrs = build_csrs(read_rows(xlsx)) if xlsx.exists() else []
    return with_commission(csrs, load(output_dir).csrs) if include_commission else csrs


def save_source(output_dir: Path, text: str) -> Path:
    """Copie locale du texte integral (output/csr/publications/<id>.md), ecriture atomique."""
    import os
    path = source_paths(output_dir)[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def _file_country(document: str, filename: str, url: str) -> str | None:
    return document_country(document, url if "filename=" in url else f"?filename={filename}", "")


def fetch_missing(fetcher: Any, countries: list[str], page_url: str = PUBLICATION_URL,
                  reader: Callable[[bytes], tuple[int, str]] | None = None) -> str:
    """Texte (format de l'etage livrables) des PDF anglais des `countries`, telecharges depuis la page de publication.

    `fetcher` : PoliteFetcher (ou equivalent : get(url, cache=...) -> objet avec `content`). `reader` : extraction du
    texte (defaut : pdf_text des livrables). Un fichier illisible ou introuvable est ignore (pays toujours manquant).
    """
    from tsi_agent.http import FetchError
    from tsi_agent.parsers.publication import parse_publication, select_files

    if reader is None:
        from tsi_agent.components.deliverables import pdf_text
        reader = pdf_text
    wanted = set(countries)
    parse = parse_publication(fetcher.get(page_url).content)
    sections: list[str] = []
    for f in select_files(parse):
        country = _file_country(f.document, f.filename, f.url)
        if country not in wanted or f.language not in (None, "en"):
            continue
        try:
            data = fetcher.get(f.url, cache=False).content
            if not data.startswith(b"%PDF"):
                continue
            _, text = reader(data)
        except (FetchError, Exception):  # pdfplumber leve des types varies sur un fichier abime
            continue
        wanted.discard(country)
        sections.append(f"## {f.document or f.filename}\n\nSource: {f.url}\n\n{text}")
    return "\n\n".join(sections)


def merge_texts(existing: str | None, added: str) -> str:
    """Copie locale completee : documents ajoutes a la suite du texte existant (front matter garde)."""
    if not added:
        return existing or ""
    if not existing:
        return f"# {PUBLICATION_ID}\n\n{added}\n"
    return existing.rstrip("\n") + "\n\n" + added + "\n"


def country_names(isos: list[str]) -> str:
    return ", ".join(name_from_iso(c) for c in isos)
