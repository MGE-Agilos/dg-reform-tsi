# Rapport d'anomalies : plan d'implémentation (plan 1 sur 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** fusionner `feature/sitemap-discovery`, rafraîchir le corpus, puis produire `python -m tsi_agent anomalies`, qui écrit un CSV et un résumé Markdown des anomalies de publication.

**Architecture :** un module déterministe `src/tsi_agent/anomalies.py`, sans LLM ni réseau.
- Entrées : le front matter des fichiers `output/projects/*.md` (lu avec `render.parse_front_matter`) et `output/state/deliverables.json`.
- Doublons : la mesure de similarité de la fusion floue, `fuzzy.title_similarity` avec le seuil `fuzzy.THRESHOLD`.
- Sortie : `output/reports/anomalies.csv` et `output/reports/anomalies.md`.

**Tech Stack :** Python 3.12, `csv`, PyYAML (déjà présent), pytest.

**Spec :** `docs/superpowers/specs/2026-10-12-csr-linking-anomalies-design.md`, sections 0 et C. Les plans 2 (ingestion des CSR) et 3 (liaison projet → CSR) seront écrits après celui-ci.

**Conventions :**
- tests : `.venv/Scripts/python -m pytest` depuis la racine du dépôt ;
- les tests écrivent dans `tmp_path` et ne touchent jamais `output/` ;
- messages de commit en français, terminés par la ligne `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

---

### Task 1 : fusionner `feature/sitemap-discovery`

**Files :** modifié : `src/tsi_agent/__main__.py`, le seul fichier touché par les deux branches.

- [ ] **Step 1 : fusionner**

```bash
git merge --no-ff feature/sitemap-discovery -m "Merge feature/sitemap-discovery into feature/prod-deploy"
```

En cas de conflit dans `__main__.py`, garder **les deux** jeux de sous-commandes :
- de `sitemap-discovery` : options `--discover-only` et sitemap de `run`, etc. ;
- de `prod-deploy` : `trial-bundle` et `prod-export`, ainsi que leurs blocs `if args.command == ...`.

Ensuite, `git add src/tsi_agent/__main__.py` puis `git commit --no-edit`.

- [ ] **Step 2 : lancer toute la suite**

Run : `.venv/Scripts/python -m pytest -q`
Expected : tout passe. On partait de 473 tests sur `prod-deploy`, plus ceux de `sitemap-discovery`.

- [ ] **Step 3 : vérifier les commandes**

Run : `.venv/Scripts/python -m tsi_agent --help`
Expected : `run`, `index`, `deliverables`, `trial-bundle` et `prod-export` sont listées.

---

### Task 2 : rafraîchir le corpus (étape d'exploitation, sans code)

Étape longue : elle appelle le LLM via le CLI Claude pour environ 165 nouvelles pages.

Avant de commencer, arrêter le serveur de chat local : Qdrant `~/tsi-vectors` n'accepte qu'un processus à la fois.

- [ ] **Step 1 : découverte seule, pour contrôler le volume**

Run : `.venv/Scripts/python -m tsi_agent run --dry-run`
Expected : le journal annonce les pages trouvées par le sitemap et le nombre de projets à extraire (autour de 165).

- [ ] **Step 2 : extraction**

Run : `.venv/Scripts/python -m tsi_agent run`, en arrière-plan.
Expected : un code de sortie 0. Le nombre de fichiers dans `output/projects` change (fusions de projets « titre seul » et nouvelles pages).

- [ ] **Step 3 : livrables des nouveaux projets**

Run : `.venv/Scripts/python -m tsi_agent deliverables`

- [ ] **Step 4 : vectorisation**

Run : `.venv/Scripts/python -m tsi_agent index`

- [ ] **Step 5 : noter les nouveaux comptes**

Run :

```bash
.venv/Scripts/python -c "import pathlib,yaml;from tsi_agent.render import parse_front_matter as p;fs=[p(f.read_text(encoding='utf-8'))[0] for f in pathlib.Path('output/projects').glob('*.md')];print(len(fs),sum(bool(f.get('detail_page')) for f in fs))"
```

Expected : deux nombres, le total des projets et ceux qui ont une page détail. Avant la fusion : 2100 et 668. Les noter pour le compte rendu.

---

### Task 3 : anomalies par projet

**Files :**
- créé : `src/tsi_agent/anomalies.py`
- test : `tests/test_anomalies.py`

- [ ] **Step 1 : écrire les tests qui doivent échouer**

```python
import csv
import json

from tsi_agent.anomalies import Anomaly, detect, load_projects, load_publications, project_anomalies, run


def _project(pid, **kw):
    base = {"project_id": pid, "title": pid.replace("-", " ").title(), "programme": "TSI", "year": 2022,
            "countries": ["GR"], "detail_page": True, "results": "achieved", "deliverables": 1,
            "source_urls": [f"https://example.eu/{pid}"], "first_seen": "2026-09-30"}
    base.update(kw)
    return base


def _types(rows):
    return sorted((r.type, r.severity) for r in rows)


def test_tsi_not_published_severity_by_age():
    old = project_anomalies(_project("a", detail_page=False, year=2024), current_year=2026)
    last = project_anomalies(_project("b", detail_page=False, year=2025), current_year=2026)
    new = project_anomalies(_project("c", detail_page=False, year=2026), current_year=2026)
    unknown = project_anomalies(_project("d", detail_page=False, year=None), current_year=2026)
    assert _types(old) == [("tsi_not_published", "high")]
    assert _types(last) == [("tsi_not_published", "medium")]
    assert _types(new) == [("tsi_not_published", "low")]
    assert _types(unknown) == [("tsi_not_published", "medium")]


def test_srsp_without_page_is_info_only():
    rows = project_anomalies(_project("s", programme="SRSP", detail_page=False, results=None, deliverables=None),
                             current_year=2026)
    assert _types(rows) == [("srsp_title_only", "info")]


def test_published_without_deliverable():
    achieved = project_anomalies(_project("a", deliverables=None), current_year=2026)
    expected = project_anomalies(_project("b", deliverables=None, results="expected"), current_year=2026)
    assert _types(achieved) == [("published_no_deliverable", "medium")]
    assert _types(expected) == [("published_no_deliverable", "low")]


def test_missing_results():
    rows = project_anomalies(_project("a", results=None), current_year=2026)
    assert _types(rows) == [("missing_results", "low")]


def test_clean_project_has_no_anomaly():
    assert project_anomalies(_project("a"), current_year=2026) == []


def test_row_fields():
    (row,) = project_anomalies(_project("a", detail_page=False, countries=["HR", "SI"]), current_year=2026)
    assert row == Anomaly(project_id="a", title="A", programme="TSI", year=2022, countries="HR,SI",
                          type="tsi_not_published", severity="high", detail="no detail page (first seen 2026-09-30)",
                          url="https://example.eu/a")
```

- [ ] **Step 2 : vérifier l'échec**

Run : `.venv/Scripts/python -m pytest tests/test_anomalies.py -q`
Expected : FAIL, `ModuleNotFoundError: No module named 'tsi_agent.anomalies'`.

- [ ] **Step 3 : implémenter**

`src/tsi_agent/anomalies.py` :

```python
"""Rapport d'anomalies : projets a publier, pages sans livrable, resultats manquants, livrables orphelins, doublons
probables. Deterministe (ni LLM ni reseau) : lit output/projects/*.md et output/state/deliverables.json."""

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date
from itertools import combinations
from pathlib import Path

from tsi_agent.fuzzy import THRESHOLD, title_similarity
from tsi_agent.render import parse_front_matter

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
MD_ROWS_PER_TYPE = 20


@dataclass(frozen=True)
class Anomaly:
    project_id: str
    title: str
    programme: str
    year: int | None
    countries: str
    type: str
    severity: str
    detail: str
    url: str


def _row(p: dict, type_: str, severity: str, detail: str = "") -> Anomaly:
    urls = p.get("source_urls") or []
    return Anomaly(p["project_id"], p.get("title") or "", p.get("programme") or "", p.get("year"),
                   ",".join(p.get("countries") or []), type_, severity, detail, urls[0] if urls else "")


def _age_severity(year: int | None, current_year: int) -> str:
    """Un projet TSI non publie est d'autant plus anormal qu'il est ancien ; annee inconnue = medium."""
    if year is None or year == current_year - 1:
        return "medium"
    return "high" if year <= current_year - 2 else "low"


def project_anomalies(p: dict, current_year: int) -> list[Anomaly]:
    """Anomalies d'un projet seul (front matter)."""
    if not p.get("detail_page"):
        if p.get("programme") == "TSI":
            return [_row(p, "tsi_not_published", _age_severity(p.get("year"), current_year),
                         f"no detail page (first seen {p.get('first_seen')})")]
        return [_row(p, "srsp_title_only", "info", "known from the programme PDF list only")]
    out = []
    if not p.get("deliverables"):
        out.append(_row(p, "published_no_deliverable", "medium" if p.get("results") == "achieved" else "low",
                        f"results: {p.get('results') or 'none'}"))
    if not p.get("results"):
        out.append(_row(p, "missing_results", "low", "no results section on the detail page"))
    return out
```

Ajouter aussi tout de suite `load_projects`, `load_publications`, `detect` et `run`. Ils sont testés aux tâches 4 et 5, mais l'import du test les exige :

```python
def load_projects(output_dir: Path) -> list[dict]:
    """Front matter de chaque projet (fichiers sans front matter ignores)."""
    projects = []
    for path in sorted((output_dir / "projects").glob("*.md")):
        try:
            front, _ = parse_front_matter(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if front and front.get("project_id"):
            projects.append(front)
    return projects


def load_publications(output_dir: Path) -> list[dict]:
    path = output_dir / "state" / "deliverables.json"
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("publications", {}).values())


def orphan_deliverables(publications: list[dict], known: set[str]) -> list[Anomaly]:
    """Publications dont aucun projet reference (id ou alias) n'existe."""
    out = []
    for pub in publications:
        ids = pub.get("project_ids") or []
        if any(i in known for i in ids):
            continue
        refs = f"references unknown {', '.join(ids)}" if ids else "references no project"
        out.append(Anomaly("", pub.get("title") or "", "", None, "", "orphan_deliverable", "medium",
                           f"{pub.get('publication_id')}: {refs}", pub.get("url") or ""))
    return out


def possible_duplicates(projects: list[dict]) -> list[Anomaly]:
    """Paires de projets d'un meme programme et d'un pays commun aux titres presque identiques."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in projects:
        for country in p.get("countries") or []:
            groups[(p.get("programme"), country)].append(p)
    seen: set[tuple[str, str]] = set()
    out = []
    for members in groups.values():
        for a, b in combinations(members, 2):
            first, second = sorted((a, b), key=lambda p: p["project_id"])
            key = (first["project_id"], second["project_id"])
            if key in seen:
                continue
            score = title_similarity(first.get("title") or "", second.get("title") or "")
            if score >= THRESHOLD:
                seen.add(key)
                out.append(_row(first, "possible_duplicate", "medium",
                                f"similar to {second['project_id']} ({second.get('year')}, score {score:.2f})"))
    return out


def detect(projects: list[dict], publications: list[dict], current_year: int) -> list[Anomaly]:
    known = {p["project_id"] for p in projects} | {a for p in projects for a in p.get("aliases") or []}
    rows = [a for p in projects for a in project_anomalies(p, current_year)]
    rows += orphan_deliverables(publications, known)
    rows += possible_duplicates(projects)
    return sorted(rows, key=lambda a: (SEVERITY_ORDER[a.severity], a.type, a.programme, a.year or 0, a.project_id))


def run(output_dir: Path, out_dir: Path, current_year: int | None = None) -> dict:
    raise NotImplementedError  # remplace a la tache 5
```

- [ ] **Step 4 : vérifier**

Run : `.venv/Scripts/python -m pytest tests/test_anomalies.py -q`
Expected : les 6 tests passent.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/anomalies.py tests/test_anomalies.py
git commit -m "feat(anomalies): anomalies par projet (TSI non publie, page sans livrable, resultats manquants)"
```

---

### Task 4 : livrables orphelins, doublons et tri

**Files :** test : `tests/test_anomalies.py`, à compléter.

- [ ] **Step 1 : ajouter les tests**

```python
def test_orphan_deliverables_use_ids_and_aliases():
    projects = [_project("a", aliases=["old-a"])]
    pubs = [{"publication_id": "p1", "title": "P1", "url": "u1", "project_ids": ["a"]},
            {"publication_id": "p2", "title": "P2", "url": "u2", "project_ids": ["old-a"]},
            {"publication_id": "p3", "title": "P3", "url": "u3", "project_ids": ["ghost"]},
            {"publication_id": "p4", "title": "P4", "url": "u4", "project_ids": []}]
    rows = [r for r in detect(projects, pubs, 2026) if r.type == "orphan_deliverable"]
    assert [r.detail for r in rows] == ["p3: references unknown ghost", "p4: references no project"]
    assert all(r.project_id == "" and r.severity == "medium" for r in rows)


def test_possible_duplicates_need_same_programme_and_country():
    projects = [_project("x1", title="Support to the reform of the Greek tax administration"),
                _project("x2", title="Support to the reform of the tax administration of Greece", year=2023),
                _project("x3", title="Support to the reform of the tax administration", countries=["HR"]),
                _project("x4", title="Support to the reform of the Greek tax administration", programme="SRSP")]
    rows = [r for r in detect(projects, [], 2026) if r.type == "possible_duplicate"]
    assert [(r.project_id, r.detail.split(" (")[0]) for r in rows] == [("x1", "similar to x2")]


def test_detect_sorts_by_severity_then_type():
    projects = [_project("z", results=None), _project("y", detail_page=False, year=2021),
                _project("w", programme="SRSP", detail_page=False)]
    assert [(r.severity, r.type) for r in detect(projects, [], 2026)] == [
        ("high", "tsi_not_published"), ("low", "missing_results"), ("info", "srsp_title_only")]
```

- [ ] **Step 2 : lancer**

Run : `.venv/Scripts/python -m pytest tests/test_anomalies.py -q`
Expected : PASS, puisque le code a été écrit à la tâche 3.
- Si `test_possible_duplicates…` échoue parce que le score de x1/x2 est sous `THRESHOLD`, afficher `title_similarity` des deux titres. Remplacer alors x2 par un titre plus proche (`"Support to the reform of Greek tax administration"`) **sans** toucher au seuil.
- Si x1/x4 matche, c'est le filtre de programme qui manque : corriger `possible_duplicates`.

- [ ] **Step 3 : commit**

```bash
git add tests/test_anomalies.py src/tsi_agent/anomalies.py
git commit -m "test(anomalies): livrables orphelins, doublons probables, tri par gravite"
```

---

### Task 5 : écriture du CSV et du Markdown

**Files :**
- modifié : `src/tsi_agent/anomalies.py`
- test : `tests/test_anomalies.py`

- [ ] **Step 1 : tests de bout en bout sur un dossier de sortie factice**

```python
PROJECT_MD = """---
project_id: {pid}
title: {title}
programme: TSI
year: 2021
countries:
- GR
detail_page: false
source_urls:
- https://example.eu/{pid}
first_seen: '2026-09-30'
---

# {title}
"""


def _output_dir(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "state").mkdir()
    (out / "projects" / "alpha.md").write_text(PROJECT_MD.format(pid="alpha", title="Alpha é"), encoding="utf-8")
    (out / "projects" / "broken.md").write_text("no front matter", encoding="utf-8")
    (out / "state" / "deliverables.json").write_text(json.dumps({"publications": {
        "p1": {"publication_id": "p1", "title": "P1", "url": "u1", "project_ids": ["ghost"]}}}), encoding="utf-8")
    return out


def test_loaders(tmp_path):
    out = _output_dir(tmp_path)
    assert [p["project_id"] for p in load_projects(out)] == ["alpha"]
    assert [p["publication_id"] for p in load_publications(out)] == ["p1"]
    assert load_publications(tmp_path / "missing") == []


def test_run_writes_csv_and_markdown(tmp_path):
    out = _output_dir(tmp_path)
    reports = tmp_path / "reports"
    summary = run(out, reports, current_year=2026)
    assert summary == {"total": 2, "by_type": {"tsi_not_published": 1, "orphan_deliverable": 1},
                       "by_severity": {"high": 1, "medium": 1}}
    raw = (reports / "anomalies.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM : Excel lit l'UTF-8
    with (reports / "anomalies.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ["project_id", "title", "programme", "year", "countries", "type", "severity",
                             "detail", "url"]
    assert rows[0]["title"] == "Alpha é" and rows[0]["year"] == "2021"
    assert rows[1]["year"] == ""
    md = (reports / "anomalies.md").read_text(encoding="utf-8")
    assert "# Publication anomalies" in md
    assert "| tsi_not_published | high | 1 |" in md
    assert "## tsi_not_published (1)" in md
    assert "Alpha é" in md
```

- [ ] **Step 2 : vérifier l'échec**

Run : `.venv/Scripts/python -m pytest tests/test_anomalies.py -q -k "run_writes or loaders"`
Expected : `test_run_writes_csv_and_markdown` échoue sur `NotImplementedError`.

- [ ] **Step 3 : implémenter, en remplaçant le `run` provisoire**

```python
def _markdown(rows: list[Anomaly], current_year: int) -> str:
    lines = ["# Publication anomalies", "",
             f"Generated {date.today().isoformat()} (reference year {current_year}). {len(rows)} anomalies.", "",
             "## By type and severity", "", "| type | severity | count |", "|---|---|---|"]
    by_type_sev = Counter((r.type, r.severity) for r in rows)
    for (type_, sev), n in sorted(by_type_sev.items(), key=lambda kv: (SEVERITY_ORDER[kv[0][1]], kv[0][0])):
        lines.append(f"| {type_} | {sev} | {n} |")
    lines += ["", "## By programme and year", "", "| programme | year | count |", "|---|---|---|"]
    for (prog, year), n in sorted(Counter((r.programme or "-", r.year) for r in rows).items(),
                                  key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        lines.append(f"| {prog} | {year if year is not None else '-'} | {n} |")
    by_type: dict[str, list[Anomaly]] = defaultdict(list)
    for r in rows:
        by_type[r.type].append(r)
    for type_, items in by_type.items():
        lines += ["", f"## {type_} ({len(items)})", ""]
        for r in items[:MD_ROWS_PER_TYPE]:
            label = f"{r.title} ({r.project_id})" if r.project_id else r.title
            meta = " · ".join(str(v) for v in (r.programme, r.year, r.countries, r.severity, r.detail) if v)
            lines.append(f"- [{label}]({r.url}) — {meta}" if r.url else f"- {label} — {meta}")
        if len(items) > MD_ROWS_PER_TYPE:
            lines.append(f"- … {len(items) - MD_ROWS_PER_TYPE} more in anomalies.csv")
    return "\n".join(lines) + "\n"


def write_report(rows: list[Anomaly], out_dir: Path, current_year: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "anomalies.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[f.name for f in fields(Anomaly)])
        writer.writeheader()
        writer.writerows(asdict(r) for r in rows)
    (out_dir / "anomalies.md").write_text(_markdown(rows, current_year), encoding="utf-8")
    return {"total": len(rows), "by_type": dict(Counter(r.type for r in rows)),
            "by_severity": dict(Counter(r.severity for r in rows))}


def run(output_dir: Path, out_dir: Path, current_year: int | None = None) -> dict:
    year = current_year or date.today().year
    rows = detect(load_projects(output_dir), load_publications(output_dir), year)
    return write_report(rows, out_dir, year)
```

Note : `csv.DictWriter` écrit `None` sous forme de chaîne vide, d'où `year == ""` pour un livrable orphelin.

- [ ] **Step 4 : vérifier**

Run : `.venv/Scripts/python -m pytest tests/test_anomalies.py -q`
Expected : tous les tests passent. L'ordre de `by_type` dans le résumé suit le tri (high avant medium), ce qui correspond au dictionnaire attendu.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/anomalies.py tests/test_anomalies.py
git commit -m "feat(anomalies): rapport CSV (BOM pour Excel) et resume Markdown"
```

---

### Task 6 : commande CLI, exécution réelle, documentation

**Files :**
- modifié : `src/tsi_agent/__main__.py`
- modifié : `README.md`, section des commandes
- test : `tests/test_cli.py`

- [ ] **Step 1 : test CLI**

Ajouter à `tests/test_cli.py`, en suivant le style des tests existants. Lire d'abord le haut du fichier pour savoir comment `--settings` et `--sources` sont fournis : il faut un `settings.yaml` temporaire dont `output_dir` pointe vers `tmp_path`.

```python
def test_anomalies_command_writes_report(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "projects" / "a.md").write_text(
        "---\nproject_id: a\ntitle: A\nprogramme: TSI\nyear: 2020\ndetail_page: false\n---\n\n# A\n",
        encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {out.as_posix()}\n", encoding="utf-8")
    reports = tmp_path / "reports"
    code = cli.main(["--settings", str(settings), "anomalies", "--out", str(reports), "--year", "2026"])
    assert code == 0
    assert (reports / "anomalies.csv").exists() and (reports / "anomalies.md").exists()
```

`main` vient de `tsi_agent.__main__` et `--sources` garde sa valeur par défaut, `config/sources.yaml`, qui existe dans le dépôt.

- [ ] **Step 2 : vérifier l'échec**

Run : `.venv/Scripts/python -m pytest tests/test_cli.py -q -k anomalies`
Expected : FAIL. argparse sort avec le code 2 : `invalid choice: 'anomalies'`.

- [ ] **Step 3 : ajouter la commande**

Dans `main()`, après le parseur `prod-export` :

```python
    anom = sub.add_parser("anomalies",
                          help="rapport d'anomalies de publication (CSV + Markdown), sans LLM ni réseau")
    anom.add_argument("--out", help="dossier du rapport (défaut : <output_dir>/reports)")
    anom.add_argument("--year", type=int, help="année de référence pour la gravité (défaut : année courante)")
```

Puis, après le bloc `if args.command == "prod-export":` :

```python
    if args.command == "anomalies":
        from tsi_agent.anomalies import run as run_anomalies
        out = Path(args.out) if args.out else Path(settings.output_dir) / "reports"
        print(json.dumps(run_anomalies(Path(settings.output_dir), out, args.year), indent=2, ensure_ascii=False))
        print(f"Rapport : {out / 'anomalies.csv'} et {out / 'anomalies.md'}")
        return 0
```

- [ ] **Step 4 : vérifier**

Run : `.venv/Scripts/python -m pytest -q`
Expected : toute la suite passe.

- [ ] **Step 5 : exécution sur le corpus réel**

Run : `.venv/Scripts/python -m tsi_agent anomalies`
Expected : un résumé JSON. Vérifications de cohérence :
- `tsi_not_published` est inférieur aux 565 d'avant la fusion ;
- `srsp_title_only` est inférieur ou égal à 867 ;
- `published_no_deliverable` est de l'ordre de 185.

Ouvrir `output/reports/anomalies.md` et contrôler à la main 5 lignes `possible_duplicate`. Si plus de la moitié sont de vrais projets distincts (phases successives par exemple), le signaler dans le compte rendu **sans** changer le seuil.

- [ ] **Step 6 : documentation**

Dans `README.md`, ajouter `anomalies` à la liste des commandes : une ligne qui renvoie aux types d'anomalies de la spec.

- [ ] **Step 7 : commit**

```bash
git add src/tsi_agent/__main__.py tests/test_cli.py README.md
git commit -m "feat(cli): commande anomalies (rapport dans output/reports)"
```
