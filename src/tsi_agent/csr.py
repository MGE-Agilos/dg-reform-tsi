"""Recommandations par pays (CSR) du Semestre europeen : base ECFIN (export Qlik) -> une fiche Markdown par CSR.

L'export a une ligne par CSR ("1") ou sous-partie ("1.2") et par version annuelle d'evaluation ("2025 - Semester
Cycle (June)") : texte et domaines de la derniere version, historique des evaluations garde. Les domaines CSR
(taxonomie ECFIN) restent dans le front matter et le texte : ce ne sont pas les domaines des projets.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from tsi_agent.countries import iso_from_name, name_from_iso
from tsi_agent.render import render

SOURCE_URL = "https://ec.europa.eu/economy_finance/country-specific-recommendations-database/"
YEAR_FROM, YEAR_TO = 2016, 2025  # SRSP (2017) et TSI (2021) : CSR des annees precedant les demandes
COLUMNS = ("Country", "Year", "CSR Nbr", "Version", "Text", "Policy Area(s)", "EAR(s)", "MIP",
           "Annual/Multiannual", "Assessment")
_VERSION_YEAR = re.compile(r"^\s*(\d{4})")
_EMPTY = {"", "-"}


def _clean(value) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return "" if text in _EMPTY else text


def _areas(value) -> list[str]:
    return [a for a in (_clean(p) for p in str(value or "").split(";")) if a]


def _version_year(version: str) -> int:
    match = _VERSION_YEAR.match(version)
    return int(match.group(1)) if match else 0


def _number_key(number: str) -> tuple[int, ...]:
    return tuple(int(n) if n.isdigit() else 0 for n in number.split("."))


@dataclass
class Part:
    """Une CSR complete ou une sous-partie, a sa derniere version."""
    part_id: str
    text: str
    policy_areas: list[str]
    assessments: list[tuple[int, str]]  # (annee de la version, evaluation), chronologique

    @property
    def assessment(self) -> str | None:
        return self.assessments[-1][1] if self.assessments else None


@dataclass
class Csr:
    country: str
    year: int
    number: str
    full: Part | None
    subparts: list[Part] = field(default_factory=list)

    @property
    def csr_id(self) -> str:
        return f"csr-{self.country}-{self.year}-{self.number}"

    @property
    def title(self) -> str:
        return f"{name_from_iso(self.country)} {self.year} CSR {self.number}"

    @property
    def text(self) -> str:
        return self.full.text if self.full else " ".join(p.text for p in self.subparts if p.text)

    @property
    def policy_areas(self) -> list[str]:
        return self.full.policy_areas if self.full else []

    @property
    def assessments(self) -> list[tuple[int, str]]:
        return self.full.assessments if self.full else []

    @property
    def assessment(self) -> str | None:
        return self.full.assessment if self.full else None


def read_rows(path: Path) -> list[dict]:
    """Lignes de l'export xlsx (premiere ligne = en-tetes)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        rows = wb.active.iter_rows(values_only=True)
        header = [str(h).strip() for h in next(rows)]
        return [dict(zip(header, r)) for r in rows if any(v is not None for v in r)]
    finally:
        wb.close()


def _part(part_id: str, versions: list[dict]) -> Part:
    versions = sorted(versions, key=lambda r: _version_year(_clean(r.get("Version"))))
    last = versions[-1]
    assessments = [(_version_year(_clean(r.get("Version"))), _clean(r.get("Assessment"))) for r in versions]
    return Part(part_id, _clean(last.get("Text")), _areas(last.get("Policy Area(s)")),
                [(y, a) for y, a in assessments if a])


def build_csrs(rows: list[dict], year_from: int = YEAR_FROM, year_to: int = YEAR_TO) -> list[Csr]:
    """CSR des Etats membres (pays inconnus, dont le Royaume-Uni, ignores) adoptees entre year_from et year_to."""
    grouped: dict[tuple[str, int, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        iso = iso_from_name(_clean(r.get("Country")))
        year, number = _clean(r.get("Year")), _clean(r.get("CSR Nbr"))
        if not iso or not year.isdigit() or not number or not year_from <= int(year) <= year_to:
            continue
        main, _, sub = number.partition(".")
        grouped[(iso, int(year), main)][number if sub and sub != "0" else main].append(r)
    csrs = []
    for (iso, year, main), parts in sorted(grouped.items()):
        prefix = f"csr-{iso}-{year}-"
        full = _part(prefix + main, parts[main]) if main in parts else None
        subs = [_part(prefix + n, parts[n]) for n in sorted((n for n in parts if n != main), key=_number_key)]
        csrs.append(Csr(iso, year, main, full, subs))
    return csrs


def _subpart_line(p: Part) -> str:
    facts = []
    if p.policy_areas:
        facts.append(f"Policy areas: {'; '.join(p.policy_areas)}")
    if p.assessment:
        facts.append(f"Assessment: {p.assessment}")
    return f"- {p.part_id}: {p.text}" + (f" ({' · '.join(facts)})" if facts else "")


def render_csr(csr: Csr) -> str:
    front = {"csr_id": csr.csr_id, "title": csr.title, "countries": [csr.country], "year": csr.year,
             "number": csr.number, "policy_areas": csr.policy_areas, "assessment": csr.assessment,
             "subparts": [p.part_id for p in csr.subparts], "source_url": SOURCE_URL}
    lines = [f"# {csr.title}", "", csr.text]
    if csr.policy_areas:
        lines += ["", f"Policy areas: {'; '.join(csr.policy_areas)}."]
    if csr.assessments:
        last_year, last = csr.assessments[-1]
        history = "; ".join(f"{y}: {a}" for y, a in csr.assessments)
        lines += ["", "## Assessment", "", f"Latest assessment ({last_year}): {last}. History: {history}."]
    if csr.subparts:
        lines += ["", "## Sub-parts", ""] + [_subpart_line(p) for p in csr.subparts]
    return render(front, "\n".join(lines))


def write_csrs(csrs: list[Csr], out_dir: Path) -> int:
    """Une fiche par CSR dans out_dir (fiches csr-*.md precedentes supprimees, autres fichiers gardes)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("csr-*.md"):
        old.unlink()
    for csr in csrs:
        (out_dir / f"{csr.csr_id}.md").write_text(render_csr(csr), encoding="utf-8", newline="\n")
    return len(csrs)
