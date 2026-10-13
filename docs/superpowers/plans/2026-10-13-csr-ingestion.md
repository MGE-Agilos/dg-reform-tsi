# Ingestion des CSR — plan d'implémentation (plan 2/3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :** ajouter les recommandations par pays (CSR, Semestre européen, 2016-2025, EU27) au corpus local et de production, avec un outil de recherche `search_csrs` dans le chatbot (local et plateforme).

**Architecture :**
- `csr_fetch.py` reproduit l'export Qlik de la base ECFIN et écrit `output/csr/csr_full.xlsx`.
- `csr.py` regroupe les lignes par CSR et produit une fiche Markdown par CSR, `output/csr/csr-<ISO>-<année>-<n>.md`. La fiche contient le texte complet de la CSR, une section Assessment et une section Sub-parts.
- `chunking.chunk_file` reconnaît `csr_id` et produit des morceaux de type `doc_type: csr`. Le chargeur local et `prod-export` lisent le dossier `csr`.
- Le chatbot local reçoit un composant `CsrSearch`. La plateforme reçoit un `PipelineTool` hybride filtré sur `doc_type: csr`.

**Tech stack :** Python 3.12, openpyxl (lecture xlsx), websockets (moteur Qlik), httpx (déjà présent), Haystack, Qdrant local, YAML de pipeline deepset.

**Spec :** `docs/superpowers/specs/2026-10-12-csr-linking-anomalies-design.md`, section A. Le plan 3, la liaison projet → CSR, utilisera les sous-parties `csr-<ISO>-<année>-<n>.<m>` produites ici.

**Choix de conception, précisés par rapport à la spec :**
- **Morceaux indexés.** Les sous-parties sont souvent des fragments de phrase, par exemple « and pension systems, ». Ce ne sont donc pas des morceaux séparés. Une fiche produit 2 ou 3 morceaux :
  - l'introduction, qui contient le texte complet de la CSR ;
  - la section `Assessment` ;
  - la section `Sub-parts`, qui liste chaque sous-partie avec son identifiant, son domaine et son évaluation.
- **Domaines CSR.** La taxonomie ECFIN (« Healthcare », « Budgetary framework… ») n'est pas celle des projets (`health-and-long-term-care`). Elle reste dans le front matter (`policy_areas`) et dans le texte, jamais dans la métadonnée `policy_area`, qui sert de filtre aux projets.
- **`CHUNKER_VERSION` inchangé.** On ajoute seulement un type de fichier et les morceaux existants ne changent pas. `index` vectorise les nouvelles fiches sans tout refaire.

**Conventions :**
- Tests : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`. Tests dans `tmp_path`, jamais dans `output/`.
- Commits en français, terminés par `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

---

### Task 1 : dépendances

**Fichiers :** modifié `pyproject.toml`.

- [ ] **Step 1 : ajouter les dépendances**

Dans `[project] dependencies`, après `"trafilatura>=1.12",` :

```toml
    "openpyxl>=3.1",
    "websockets>=13",
```

`websockets>=13` est nécessaire : l'argument `additional_headers` de `websockets.connect` date de la version 13.

- [ ] **Step 2 : installer**

Run : `.venv/Scripts/python -m pip install "openpyxl>=3.1" "websockets>=13"`
Puis : `.venv/Scripts/python -c "import openpyxl, websockets; print(openpyxl.__version__, websockets.__version__)"`
Expected : deux numéros de version, avec websockets en version 13 ou plus.

- [ ] **Step 3 : commit**

```bash
git add pyproject.toml
git commit -m "build: dependances openpyxl et websockets (base CSR d'ECFIN)"
```

---

### Task 2 : modèle CSR et fiches Markdown

**Fichiers :**
- Create : `src/tsi_agent/csr.py`
- Test : `tests/test_csr.py`

- [ ] **Step 1 : tests en échec**

```python
import openpyxl

from tsi_agent.chunking import chunk_file
from tsi_agent.csr import COLUMNS, build_csrs, read_rows, render_csr, write_csrs
from tsi_agent.render import parse_front_matter


def row(country="France", year="2023", nbr="1", version="2024 - Semester Cycle (June)", text="Text.",
        areas="Healthcare;Long-term care", assessment="Some Progress"):
    return {"Country": country, "Year": year, "CSR Nbr": nbr, "Version": version, "Text": text,
            "Policy Area(s)": areas, "EAR(s)": "-", "MIP": "No", "Annual/Multiannual": "Annual",
            "Assessment": assessment}


ROWS = [
    row(nbr="1", text="Improve the  sustainability of the pension system and of healthcare.",
        version="2024 - Semester Cycle (June)", assessment="Limited Progress"),
    row(nbr="1", text="Improve the  sustainability of the pension system and of healthcare.",
        version="2025 - Semester Cycle (June)", assessment="Some Progress"),
    row(nbr="1.2", text="and of healthcare.", areas="Healthcare", version="2025 - Semester Cycle (June)",
        assessment="Substantial Progress"),
    row(nbr="1.1", text="Improve the sustainability of the pension system", areas="Pension systems  and active ageing",
        version="2025 - Semester Cycle (June)", assessment="-"),
    row(country="Czech Republic", year="2019", nbr="2.1", text="Reduce red tape.", areas="-", assessment="-"),
    row(country="United Kingdom", year="2018", nbr="1"),
    row(year="2015", nbr="1"),
    row(year="2026", nbr="1"),
]


def test_build_groups_versions_and_subparts():
    csrs = {c.csr_id: c for c in build_csrs(ROWS)}
    assert sorted(csrs) == ["csr-CZ-2019-2", "csr-FR-2023-1"]
    fr = csrs["csr-FR-2023-1"]
    assert fr.text == "Improve the sustainability of the pension system and of healthcare."
    assert fr.assessments == [(2024, "Limited Progress"), (2025, "Some Progress")]
    assert fr.assessment == "Some Progress"
    assert [p.part_id for p in fr.subparts] == ["csr-FR-2023-1.1", "csr-FR-2023-1.2"]
    assert fr.subparts[0].policy_areas == ["Pension systems and active ageing"]
    assert fr.subparts[0].assessment is None
    assert fr.policy_areas == ["Healthcare", "Long-term care"]


def test_csr_without_full_row_uses_subpart_texts():
    cz = {c.csr_id: c for c in build_csrs(ROWS)}["csr-CZ-2019-2"]
    assert cz.text == "Reduce red tape." and cz.policy_areas == [] and cz.assessment is None


def test_year_window_is_configurable():
    assert {c.year for c in build_csrs(ROWS, year_from=2015, year_to=2026)} == {2015, 2019, 2023, 2026}


def test_render_front_matter_and_sections():
    fr = {c.csr_id: c for c in build_csrs(ROWS)}["csr-FR-2023-1"]
    fm, body = parse_front_matter(render_csr(fr))
    assert fm["csr_id"] == "csr-FR-2023-1" and fm["title"] == "France 2023 CSR 1"
    assert fm["countries"] == ["FR"] and fm["year"] == 2023 and fm["number"] == "1"
    assert fm["subparts"] == ["csr-FR-2023-1.1", "csr-FR-2023-1.2"]
    assert fm["assessment"] == "Some Progress" and fm["policy_areas"] == ["Healthcare", "Long-term care"]
    assert fm["source_url"].startswith("https://")
    assert body.startswith("# France 2023 CSR 1\n\nImprove the sustainability")
    assert "## Assessment\n\nLatest assessment (2025): Some Progress. History: 2024: Limited Progress; " \
           "2025: Some Progress." in body
    assert "- csr-FR-2023-1.2: and of healthcare. (Policy areas: Healthcare · Assessment: Substantial Progress)" in body
    assert "- csr-FR-2023-1.1: Improve the sustainability of the pension system (Policy areas: Pension systems " \
           "and active ageing)" in body


def test_write_replaces_old_files_and_chunks_as_csr(tmp_path):
    out = tmp_path / "csr"
    out.mkdir()
    (out / "csr-XX-2000-9.md").write_text("old", encoding="utf-8")
    (out / "csr_full.xlsx").write_bytes(b"keep")
    assert write_csrs(build_csrs(ROWS), out) == 2
    assert sorted(p.name for p in out.glob("*.md")) == ["csr-CZ-2019-2.md", "csr-FR-2023-1.md"]
    assert (out / "csr_full.xlsx").read_bytes() == b"keep"
    docs = chunk_file("csr/csr-FR-2023-1.md", (out / "csr-FR-2023-1.md").read_text(encoding="utf-8"))
    assert [d.meta["section"] for d in docs] == ["Introduction", "Assessment", "Sub-parts"]
    meta = docs[0].meta
    assert meta["doc_type"] == "csr" and meta["doc_id"] == "csr-FR-2023-1" and meta["countries"] == ["FR"]
    assert meta["year"] == 2023 and meta["policy_area"] is None and meta["programme"] is None
    assert meta["context_header"] == "France 2023 CSR 1 — 2023 — France"
    assert docs[0].id == "csr/csr-FR-2023-1#0"


def test_read_rows_from_xlsx(tmp_path):
    path = tmp_path / "csr.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    wb.active.append([row()[c] for c in COLUMNS])
    wb.save(path)
    rows = read_rows(path)
    assert rows == [row()]
```

- [ ] **Step 2 : vérifier l'échec**

Run : `.venv/Scripts/python -m pytest tests/test_csr.py -q -p no:cacheprovider`
Expected : FAIL, `ModuleNotFoundError: No module named 'tsi_agent.csr'`.

- [ ] **Step 3 : implémenter `src/tsi_agent/csr.py`**

```python
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
```

Note : `test_write_replaces_old_files_and_chunks_as_csr` dépend de la tâche 3, le découpage par `chunk_file`. À cette étape, il échoue avec `ValueError: front matter has no project_id or doc_id`, ce qui est attendu. Les 5 autres tests passent.

- [ ] **Step 4 : vérifier**

Run : `.venv/Scripts/python -m pytest tests/test_csr.py -q -p no:cacheprovider`
Expected : 5 tests passent ; `test_write_replaces_old_files_and_chunks_as_csr` échoue sur `ValueError`.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/csr.py tests/test_csr.py
git commit -m "feat(csr): fiches CSR depuis l'export ECFIN (versions, sous-parties, EU27, 2016-2025)"
```

---

### Task 3 : découpage `csr`, chargeur local et `prod-export`

**Fichiers :**
- Modify : `src/tsi_agent/chunking.py`, dans `chunk_file` et avec une nouvelle fonction `chunk_csr`
- Modify : `src/tsi_agent/components/file_loader.py:19`, le tuple `FOLDERS`
- Modify : `src/tsi_agent/prod_export.py`, `FOLDERS` et le nom des fichiers
- Test : `tests/test_csr.py` (déjà écrit), `tests/test_prod_export.py`

- [ ] **Step 1 : test `prod-export`**

Dans `tests/test_prod_export.py`, ajouter un test qui crée un dossier `output` avec une fiche CSR. Lire d'abord le test `export` existant du fichier pour réutiliser sa façon de construire `output/`.

```python
CSR_MD = """---
csr_id: csr-FR-2023-1
title: France 2023 CSR 1
countries: [FR]
year: 2023
number: '1'
policy_areas: [Healthcare]
assessment: Some Progress
subparts: [csr-FR-2023-1.1]
source_url: https://ec.europa.eu/economy_finance/country-specific-recommendations-database/
---

# France 2023 CSR 1

Improve the sustainability of the pension system.

## Sub-parts

- csr-FR-2023-1.1: Improve the sustainability of the pension system (Assessment: Some Progress)
"""


def test_export_includes_csr_files(tmp_path):
    output = tmp_path / "output"
    (output / "csr").mkdir(parents=True)
    (output / "csr" / "csr-FR-2023-1.md").write_text(CSR_MD, encoding="utf-8")
    out = tmp_path / "build" / "files"
    manifest = export(output, out)
    assert manifest["files_by_type"] == {"csr": 1} and manifest["meta_type_problems"] == []
    meta = json.loads((out / "tsi-csr-FR-2023-1.md.meta.json").read_text(encoding="utf-8"))
    assert meta == {"Topic": "TSI-BDG", "corpus": CORPUS, "doc_type": "csr", "doc_id": "csr-FR-2023-1",
                    "title": "France 2023 CSR 1", "countries": ["FR"], "year": 2023, "projects": [],
                    "source_url": "https://ec.europa.eu/economy_finance/country-specific-recommendations-database/",
                    "chunks": 2}
    text = (out / "tsi-csr-FR-2023-1.md").read_text(encoding="utf-8")
    assert text.startswith("# France 2023 CSR 1 — 2023 — France — Introduction\n")
    assert "Document id: csr-FR-2023-1" in text and "Detail page" not in text
```

- [ ] **Step 2 : vérifier l'échec**

Run : `.venv/Scripts/python -m pytest tests/test_csr.py tests/test_prod_export.py -q -p no:cacheprovider`
Expected : `test_write_replaces_old_files_and_chunks_as_csr` échoue avec `ValueError`. `test_export_includes_csr_files` échoue sur `files_by_type == {}` : le dossier `csr` n'est pas lu.

- [ ] **Step 3 : implémenter**

Dans `src/tsi_agent/chunking.py`, ajouter avant `chunk_file` :

```python
def chunk_csr(rel_path: str, fm: dict[str, Any], body: str, **split: int) -> list[Document]:
    """Fiche CSR : texte complet (introduction), evaluation, sous-parties. Pas de domaine de projet (taxonomies
    differentes) ni de programme."""
    base: dict[str, Any] = {
        "doc_id": fm["csr_id"], "doc_type": "csr", "source_file": rel_path, "source_url": fm.get("source_url"),
        "context_header": context_header(fm), "title": fm.get("title"), "year": fm.get("year"),
        "countries": fm.get("countries"), "programme": None, "policy_area": None, "detail_page": None,
    }
    stem = rel_path.removesuffix(".md")
    docs: list[Document] = []
    for section, section_text in split_sections(body):
        for piece in split_words(section_text, **split):
            index = len(docs)
            docs.append(Document(id=f"{stem}#{index}", content=piece,
                                 meta={**base, "section": section, "chunk_index": index, "ai_generated": False}))
    return docs
```

Dans `chunk_file`, juste après le bloc `if fm.get("publication_id"):` :

```python
    if fm.get("csr_id"):
        return chunk_csr(rel_path, fm, body, **split)
```

Mettre à jour la docstring de `chunk_file` : « Morceaux d'un fichier projet, programme, livrable ou CSR. »

Dans `src/tsi_agent/components/file_loader.py` :

```python
FOLDERS = ("projects", "programme", "deliverables", "csr")
```

Dans `src/tsi_agent/prod_export.py` :

```python
FOLDERS = {"projects": "project", "programme": "programme", "deliverables": "deliverable", "csr": "csr"}
```

Dans `export()`, remplacer `name = f"{PREFIX}{doc_type}-{path.name}"` par :

```python
            name = f"{PREFIX}{doc_type}-{path.name.removeprefix(f'{doc_type}-')}"  # tsi-csr-FR-2023-1.md
```

Dans la docstring du module, remplacer « Chaque fichier projet, programme ou livrable » par « Chaque fichier projet, programme, livrable ou CSR ».

- [ ] **Step 4 : vérifier**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`
Expected : tout passe. Si un test existant vérifie `FOLDERS` du chargeur ou les noms de fichiers, c'est qu'il attend l'ancien tuple : l'ajuster et le signaler dans le compte rendu.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/chunking.py src/tsi_agent/components/file_loader.py src/tsi_agent/prod_export.py tests/test_prod_export.py
git commit -m "feat(csr): morceaux doc_type csr, indexation locale et export de production des fiches CSR"
```

---

### Task 4 : export Qlik et commandes `csr-fetch` / `csr-build`

**Fichiers :**
- Create : `src/tsi_agent/csr_fetch.py`
- Modify : `src/tsi_agent/__main__.py`
- Test : `tests/test_cli.py`

- [ ] **Step 1 : test de `csr-build`**

`csr-fetch` fait appel au réseau et n'a pas de test unitaire : on le vérifie à l'exécution, en tâche 7. Ajouter à `tests/test_cli.py` :

```python
def test_csr_build_command(tmp_path):
    import openpyxl
    from tsi_agent.csr import COLUMNS

    out = tmp_path / "output"
    (out / "csr").mkdir(parents=True)
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    wb.active.append(["France", "2023", "1", "2024 - Semester Cycle (June)", "Do X.", "Healthcare", "-", "No",
                      "Annual", "Some Progress"])
    wb.save(out / "csr" / "csr_full.xlsx")
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {out.as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-build"]) == 0
    assert (out / "csr" / "csr-FR-2023-1.md").exists()


def test_csr_build_without_export_is_a_clean_error(tmp_path, capsys):
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-build"]) == 1
    assert "csr-fetch" in capsys.readouterr().err
```

- [ ] **Step 2 : vérifier l'échec**

Run : `.venv/Scripts/python -m pytest tests/test_cli.py -q -p no:cacheprovider -k csr`
Expected : FAIL, avec `invalid choice: 'csr-build'`.

- [ ] **Step 3 : implémenter `src/tsi_agent/csr_fetch.py`**

```python
"""Export complet de la base CSR d'ECFIN (application Qlik Sense publique) : reproduit le bouton
« Export Full Database » (ExportData OOXML du tableau) et telecharge le fichier xlsx."""

import asyncio
import json
import random
from pathlib import Path

import httpx
import websockets

BASE = "dashboard.tech.ec.europa.eu/qs_digit_dashboard_mt/public"
APP = "e94667ab-79dd-4636-8ba0-1ae23901f86c"
OBJECT = "nDjMYs"  # tableau de la base complete
HEADERS = {"User-Agent": "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"}
TIMEOUT_S = 120


async def _export(out: Path) -> dict:
    xrf = "".join(random.choice("0123456789ABCDEF") for _ in range(16))
    async with httpx.AsyncClient(follow_redirects=True, headers=HEADERS, timeout=TIMEOUT_S) as http:
        await http.get(f"https://{BASE}/hub")
        res = await http.get(f"https://{BASE}/qps/csrftoken?xrfkey={xrf}", headers={"X-Qlik-Xrfkey": xrf})
        token = res.headers.get("qlik-csrf-token")
        cookie = "; ".join(f"{k}={v}" for k, v in http.cookies.items())
        url = f"wss://{BASE}/app/{APP}?qlik-csrf-token={token}"
        async with websockets.connect(url, additional_headers={**HEADERS, "Cookie": cookie}, max_size=None) as ws:
            seq = 0

            async def call(handle: int, method: str, params: list) -> dict:
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": seq, "handle": handle, "method": method,
                                          "params": params}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
                    if msg.get("id") == seq:
                        if "error" in msg:
                            raise RuntimeError(f"Qlik {method}: {msg['error']}")
                        return msg["result"]

            doc = await call(-1, "OpenDoc", [APP])
            obj = await call(doc["qReturn"]["qHandle"], "GetObject", [OBJECT])
            handle = obj["qReturn"]["qHandle"]
            layout = await call(handle, "GetLayout", [])
            size = layout["qLayout"].get("qHyperCube", {}).get("qSize", {})
            exported = await call(handle, "ExportData", ["OOXML", "/qHyperCubeDef"])
        data = await http.get(f"https://{BASE}{exported['qUrl']}")
        data.raise_for_status()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data.content)
    return {"file": str(out), "bytes": len(data.content), "rows": size.get("qcy"), "columns": size.get("qcx")}


def fetch(out: Path) -> dict:
    """Telecharge l'export complet dans `out` (ecrase) ; renvoie taille et nombre de lignes annonce par Qlik."""
    return asyncio.run(_export(out))
```

Dans `src/tsi_agent/__main__.py`, après le parseur `anomalies` :

```python
    sub.add_parser("csr-fetch", help="télécharger la base CSR d'ECFIN (export Qlik complet, xlsx)")
    csrb = sub.add_parser("csr-build", help="fiches Markdown des CSR (output/csr) depuis l'export ECFIN")
    csrb.add_argument("--year-from", type=int, default=2016)
    csrb.add_argument("--year-to", type=int, default=2025)
```

Puis, après le bloc `if args.command == "anomalies":` :

```python
    if args.command == "csr-fetch":
        from tsi_agent.csr_fetch import fetch
        print(json.dumps(fetch(Path(settings.output_dir) / "csr" / "csr_full.xlsx"), indent=2))
        return 0
    if args.command == "csr-build":
        from tsi_agent.csr import build_csrs, read_rows, write_csrs
        source = Path(settings.output_dir) / "csr" / "csr_full.xlsx"
        if not source.exists():
            print(f"ERREUR : {source} introuvable — lancer d'abord `python -m tsi_agent csr-fetch`", file=sys.stderr)
            return 1
        rows = read_rows(source)
        written = write_csrs(build_csrs(rows, args.year_from, args.year_to), source.parent)
        print(json.dumps({"rows": len(rows), "csr_files": written, "dir": str(source.parent)}, indent=2))
        return 0
```

- [ ] **Step 4 : vérifier**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`
Expected : tout passe.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/csr_fetch.py src/tsi_agent/__main__.py tests/test_cli.py
git commit -m "feat(cli): commandes csr-fetch (export Qlik ECFIN) et csr-build (fiches output/csr)"
```

---

### Task 5 : outil `search_csrs` du chatbot local

**Fichiers :**
- Modify : `src/tsi_agent/chat/tools.py`, nouvelle classe `CsrSearch` après `DeliverableSearch`
- Modify : `src/tsi_agent/chat/pipeline.py`, import et liste `tools`
- Modify : `src/tsi_agent/chat/prompt.py`, ligne Tools et règle 16
- Test : `tests/test_chat_store_tools.py`, `tests/test_chat_agent.py`

- [ ] **Step 1 : tests**

Dans `tests/test_chat_store_tools.py`, ajouter `CsrSearch` à l'import depuis `tsi_agent.chat.tools`. Ajouter aussi `from tsi_agent.qdrant_local import open_store` s'il n'est pas déjà importé : il l'est, `open_store` fait partie de l'import existant.

```python
CSRS = [
    ("csr-FR-2023-1", ["FR"], 2023, "Improve the sustainability of the pension system."),
    ("csr-HR-2019-2", ["HR"], 2019, "Reduce the administrative burden on businesses."),
    ("csr-HR-2024-1", ["HR"], 2024, "Strengthen the anti-money laundering framework."),
]


def csr_store(tmp_path):
    store = open_store(str(tmp_path), 8)
    docs = [Document(id=f"csr/{cid}#0", content=text, meta={
        "doc_id": cid, "doc_type": "csr", "title": cid, "countries": countries, "year": year, "section": "Introduction",
        "source_url": "https://ec.europa.eu/csr", "context_header": f"{cid} — {year}", "ai_generated": False,
        "programme": None, "policy_area": None}) for cid, countries, year, text in CSRS]
    docs.append(Document(id="projects/p#0", content="A project.", meta={
        "doc_id": "p", "doc_type": "project", "countries": ["HR"], "year": 2024, "section": "Introduction"}))
    store.write_documents(MockDocumentEmbedder(model="mock", dimension=8).run(documents=docs)["documents"])
    return store


def test_csr_search_filters_type_country_and_year(tmp_path):
    tool = CsrSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                     retriever=LocalQdrantEmbeddingRetriever(document_store=csr_store(tmp_path)), top_k=10)
    out = tool.run(query="x", countries=["HR"], year_from=2020, known_sources=[{"ref": 1}])
    assert [s["doc_id"] for s in out["sources"]] == ["csr-HR-2024-1"]
    assert out["sources"][0]["ref"] == 2
    assert out["result"].startswith("[2] csr-HR-2024-1 — 2024 — section: Introduction\nCSR id: csr-HR-2024-1\n")
    all_ids = {s["doc_id"] for s in tool.run(query="x")["sources"]}
    assert all_ids == {"csr-FR-2023-1", "csr-HR-2019-2", "csr-HR-2024-1"}


def test_csr_search_empty(tmp_path):
    tool = CsrSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                     retriever=LocalQdrantEmbeddingRetriever(document_store=csr_store(tmp_path)))
    assert tool.run(query="x", countries=["SE"])["result"] == "No matching CSR passages found."
```

Dans `tests/test_chat_agent.py`, `test_yaml_round_trip_and_openai_swap` :
- ajouter `"CsrSearch"` au tuple des noms attendus ;
- remplacer la liste attendue des outils par :

```python
    assert [t.name for t in tools] == ["search_projects", "find_projects", "search_deliverables", "search_csrs",
                                       "list_projects", "get_project"]
```

Dans le même fichier, le test qui vérifie le texte du prompt (vers la ligne 49, `assert "list_projects" in text and ...`) gagne la ligne :

```python
    assert "search_csrs" in text and "csr-FR-2023-1" in text
```

- [ ] **Step 2 : vérifier l'échec**

Run : `.venv/Scripts/python -m pytest tests/test_chat_store_tools.py tests/test_chat_agent.py -q -p no:cacheprovider`
Expected : `ImportError: cannot import name 'CsrSearch'`.

- [ ] **Step 3 : implémenter**

Dans `src/tsi_agent/chat/tools.py`, ajouter après la classe `DeliverableSearch` :

```python
@component
class CsrSearch(ProjectSearch):
    @component.output_types(result=str, sources=list[dict])
    def run(self, query: str, countries: list[str] | None = None, year_from: int | None = None,
            year_to: int | None = None, known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Semantic search in the country-specific recommendations (CSRs) addressed to EU Member States in the
        European Semester (2016-2025), with their sub-parts and the Commission's latest assessment of their
        implementation. Returns numbered passages [n] with the CSR id (e.g. csr-FR-2023-1), to cite.

        :param query: What to look for, in English (e.g. "sustainability of the pension system").
        :param countries: ISO country codes (e.g. ["HR", "SI"]; Greece is "GR").
        :param year_from: Keep CSRs adopted from this year on.
        :param year_to: Keep CSRs adopted up to this year.
        """
        t0 = time.monotonic()
        embedding = self.text_embedder.run(text=query)["embedding"]
        filters = build_filters(year_from, year_to, doc_type="csr", countries=countries)
        docs = self.retriever.run(query_embedding=embedding, filters=filters, top_k=self.top_k * 3)["documents"]
        docs = [d for d in docs if _in_countries(d, countries)][: self.top_k]
        start = len(known_sources or [])
        sources: list[dict[str, Any]] = []
        blocks: list[str] = []
        for i, doc in enumerate(docs, start=start + 1):
            meta = doc.meta
            source = _source(i, doc, meta.get("section"))
            source["score"] = round(doc.score, 3) if doc.score is not None else None
            sources.append(source)
            blocks.append(f"[{i}] {meta.get('context_header') or meta.get('title')} — section: {meta.get('section')}\n"
                          f"CSR id: {meta.get('doc_id')}\nURL: {meta.get('source_url')}\n"
                          f"{(doc.content or '')[:SNIPPET_CHARS]}")
        result = "\n\n".join(blocks) if blocks else "No matching CSR passages found."
        runlog.event("CsrSearch", "tool_call", "search_csrs", query=query, countries=countries, filters=filters,
                     results=len(docs), duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": result, "sources": sources}
```

Mettre à jour la docstring du module : « recherche semantique (projets, texte integral des livrables, CSR) ».

Dans `src/tsi_agent/chat/pipeline.py`, importer `CsrSearch` et insérer, juste après le `ComponentTool` de `search_deliverables` :

```python
        ComponentTool(CsrSearch(text_embedder=embedder, retriever=retriever), name="search_csrs",
                      description="Search the EU country-specific recommendations (CSRs, European Semester "
                                  "2016-2025) and the Commission's latest assessment of their implementation; "
                                  "returns numbered passages [n] with the CSR id.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
```

Dans `src/tsi_agent/chat/prompt.py`, dans la liste `Tools:`, après la ligne `search_deliverables` et ses lignes de continuation :

```
- search_csrs: semantic search in the EU country-specific recommendations (CSRs, European Semester 2016-2025),
  with their sub-parts and latest implementation assessment. Returns numbered passages [n] with the CSR id.
```

Après la règle 15, avant `Filter values:` :

```
16. For questions about country-specific recommendations (CSRs) or the European Semester, call search_csrs and
    cite the CSR id (e.g. csr-FR-2023-1) with its passage number; say that assessments come from the
    Commission's CSR database. Do not state that a project implements a CSR unless a tool result says so.
```

- [ ] **Step 4 : vérifier**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`
Expected : tout passe.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/chat/tools.py src/tsi_agent/chat/pipeline.py src/tsi_agent/chat/prompt.py tests/test_chat_store_tools.py tests/test_chat_agent.py
git commit -m "feat(chat): outil search_csrs (recommandations par pays) et regle de prompt"
```

---

### Task 6 : outil `search_csrs` sur la plateforme

**Fichiers :**
- Modify : `deploy/prod/build_pipelines.py`
- Regenerate : `deploy/prod/pipeline-tsi-chat.yaml` et `deploy/prod/pipeline-tsi-chat-ui.yaml`

- [ ] **Step 1 : description de l'outil**

Dans `build_pipelines.py`, après `DELIVERABLES = indent(...)` :

```python
CSRS = indent("""Semantic and keyword search in the country-specific recommendations (CSRs) addressed to EU Member
States in the European Semester (2016-2025), with their sub-parts and the Commission's latest assessment of their
implementation. Returns numbered passages with the CSR id. Write a precise English query naming the country and
the policy topic.""", 12)
```

- [ ] **Step 2 : prompts**

Dans `CHAT_PROMPT` et `CHAT_PROMPT_SEARCH_ONLY`, dans `Tools:`, après le bloc `search_deliverables` :

```
- search_csrs: EU country-specific recommendations (CSRs, European Semester 2016-2025). Each passage starts with
  "country year CSR n — year — country — section" and ends with the CSR id and the source URL.
```

Ajouter la règle suivante en fin de liste : n° 14 dans `CHAT_PROMPT`, n° 13 dans `CHAT_PROMPT_SEARCH_ONLY`.

```
14. For questions about country-specific recommendations (CSRs) or the European Semester, call search_csrs and
    cite the CSR id (e.g. csr-FR-2023-1) with its passage number; say that assessments come from the
    Commission's CSR database. Do not state that a project implements a CSR unless a tool result says so.
```

Pour `CHAT_PROMPT_SEARCH_ONLY`, la même règle est numérotée « 13. ».

- [ ] **Step 3 : assemblage**

Dans les deux `write_text` de `pipeline-tsi-chat.yaml` et `pipeline-tsi-chat-ui.yaml`, ajouter après `search_tool("search_deliverables", DELIVERABLES, 8, "deliverable")` :

```python
    + search_tool("search_csrs", CSRS, 6, "csr")
```

Les pipelines d'impact ne changent pas.

- [ ] **Step 4 : régénérer et valider**

Run : `.venv/Scripts/python deploy/prod/build_pipelines.py`, depuis la racine du dépôt.

Puis lancer le validateur de brouillon, qui vérifie que chaque YAML se charge en `Pipeline` Haystack avec les composants deepset simulés :

```bash
EXTRA_PKGS='C:\tmp\hk1;C:\tmp\hk2' .venv/Scripts/python "C:/Users/MGE/AppData/Local/Temp/claude/c--Users-MGE-OneDrive---Agilos-Documents-Claude-Code-Projects-DG-REFORM-Haystack-Pipeline/f8efdd32-c9a5-4bb5-90b9-b6388ae91288/scratchpad/check/validate_yaml.py" deploy/prod/pipeline-tsi-chat.yaml deploy/prod/pipeline-tsi-chat-ui.yaml
```

Expected : les deux fichiers sont « OK », et l'agent a 5 outils pour `tsi-chat` et 3 pour `tsi-chat-ui`.
- Si le validateur ne prend pas plusieurs fichiers, le lancer une fois par fichier.
- S'il est absent, le signaler : c'est un outil de brouillon hors dépôt, et l'étape est alors faite par l'humain.

Run : `git diff --stat deploy/prod/`
Expected : `build_pipelines.py`, `pipeline-tsi-chat.yaml` et `pipeline-tsi-chat-ui.yaml` modifiés. Les YAML d'impact sont inchangés.

- [ ] **Step 5 : tests et commit**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`. Expected : tout passe.

```bash
git add deploy/prod/build_pipelines.py deploy/prod/pipeline-tsi-chat.yaml deploy/prod/pipeline-tsi-chat-ui.yaml
git commit -m "feat(deploy): outil search_csrs dans les pipelines de chat de production"
```

---

### Task 7 : exécution réelle (étape d'exploitation, faite par le contrôleur)

Il faut arrêter le chatbot local avant `index`, puis le relancer.

- [ ] **Step 1 :** `.venv/Scripts/python -m tsi_agent csr-fetch`
  - Expected : environ 31 000 lignes et 10 colonnes. Le fichier `output/csr/csr_full.xlsx` fait quelques Mo.
- [ ] **Step 2 :** `.venv/Scripts/python -m tsi_agent csr-build`
  - Expected : environ 1 000 à 1 100 fiches, pour 27 pays sur 10 ans à environ 4 CSR par pays et par an.
- [ ] **Step 3 :** contrôler 2 fiches à la main, une récente (2024) et une ancienne (2016). Vérifier le texte, les sous-parties et l'historique d'évaluation, en comparant avec la base ECFIN en ligne.
- [ ] **Step 4 :** arrêter le chatbot, lancer `.venv/Scripts/python -m tsi_agent index`, puis relancer le chatbot (`chat --no-browser`).
  - Expected : environ 1 000 fichiers et 2 à 3 000 morceaux ajoutés, aucun fichier existant revectorisé.
- [ ] **Step 5 : vérification rapide sur le chatbot local.** Poser la question « What CSRs did Croatia receive on public administration since 2022, and how were they assessed? ».
  - Expected : la réponse cite des identifiants `csr-HR-2022-…` ou plus récents, avec leur évaluation.
- [ ] **Step 6 :** mettre à jour `deploy/prod/README.md` (tests de recette) avec une question CSR. Le renvoi vers la plateforme (`prod-export`, envoi, nouvelle version des pipelines de chat) se fait **sur demande de l'utilisateur seulement**.
