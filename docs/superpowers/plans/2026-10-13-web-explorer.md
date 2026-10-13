# Explorateur web TSI × CSR (lot 1) — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :** une page GitHub Pages, réservée aux testeurs connectés, pour parcourir les projets, les CSR, les liens projet → CSR et les anomalies stockés dans Supabase.

**Architecture :**
- `supabase/migrations/001_explorer.sql` crée les tables, les règles RLS et la fonction `is_tester()`.
- `src/tsi_agent/supabase_sync.py` construit les lignes depuis `output/` et les envoie à PostgREST avec la clé `service_role`, lue depuis l'environnement ou `.env`. La commande correspondante est `python -m tsi_agent supabase-sync`.
- `web/` contient une page statique : HTML, CSS et JavaScript, avec `supabase-js` v2 figé, chargé depuis jsDelivr.
- `.github/workflows/pages.yml` publie `web/`.

**Stack technique :** Python 3.12, httpx (déjà présent), pytest ; Postgres/PostgREST (Supabase) ; JavaScript ES2020 sans build.

**Spec :** `docs/superpowers/specs/2026-10-13-web-explorer-design.md`.

**Conventions :**
- tests : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`, dans `tmp_path`, sans réseau ;
- commits en français, terminés par `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` ;
- **jamais de clé `service_role` dans un fichier suivi par git** ; seule la clé `anon` va dans `web/config.js` ;
- branche de travail : `feature/web-explorer`, créée depuis `main`. Le push et la fusion sont faits par l'utilisateur, car le classifieur de permissions bloque `git push` pour l'agent.

---

### Task 1 : schéma SQL

**Fichiers :** à créer : `supabase/migrations/001_explorer.sql`

- [ ] **Step 1 : écrire le script**

```sql
-- Explorateur TSI x CSR (lot 1). A coller dans Supabase > SQL Editor. Idempotent (rejouable).
-- Lecture reservee aux testeurs (table testers) ; aucune ecriture hors service_role.

create table if not exists public.projects (
  project_id text primary key,
  title text not null,
  programme text,
  year int,
  countries text[] not null default '{}',
  policy_area text,
  results text,
  detail_page boolean not null default false,
  url text,
  summary text,
  deliverables int not null default 0,
  csrs text[] not null default '{}'
);

create table if not exists public.csrs (
  csr_id text primary key,
  country text not null,
  year int not null,
  number text not null,
  title text not null,
  text text not null,
  policy_areas text[] not null default '{}',
  assessment text,
  assessment_year int,
  source_url text
);

create table if not exists public.csr_parts (
  part_id text primary key,
  csr_id text not null references public.csrs (csr_id) on delete cascade,
  country text not null,
  year int not null,
  text text not null,
  policy_areas text[] not null default '{}',
  assessment text
);

create table if not exists public.project_csr_links (
  project_id text not null references public.projects (project_id) on delete cascade,
  part_id text not null references public.csr_parts (part_id) on delete cascade,
  level text not null check (level in ('direct', 'partial')),
  rationale text not null,
  basis text not null,
  primary key (project_id, part_id)
);

create table if not exists public.anomalies (
  id bigint generated always as identity primary key,
  project_id text,
  title text,
  programme text,
  year int,
  countries text,
  type text not null,
  severity text not null,
  detail text,
  url text
);

create table if not exists public.testers (
  email text primary key check (email = lower(email))
);

create index if not exists projects_year_idx on public.projects (year);
create index if not exists projects_countries_idx on public.projects using gin (countries);
create index if not exists csr_parts_csr_idx on public.csr_parts (csr_id);
create index if not exists links_part_idx on public.project_csr_links (part_id);
create index if not exists csrs_country_year_idx on public.csrs (country, year);
create index if not exists anomalies_type_idx on public.anomalies (type, severity);

create or replace function public.is_tester() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.testers t where t.email = lower(coalesce(auth.jwt() ->> 'email', '')));
$$;
revoke all on function public.is_tester() from public;
grant execute on function public.is_tester() to anon, authenticated;

do $$
declare t text;
begin
  foreach t in array array['projects', 'csrs', 'csr_parts', 'project_csr_links', 'anomalies', 'testers'] loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
  foreach t in array array['projects', 'csrs', 'csr_parts', 'project_csr_links', 'anomalies'] loop
    execute format('drop policy if exists tester_read on public.%I', t);
    execute format('create policy tester_read on public.%I for select to authenticated using (public.is_tester())', t);
  end loop;
end $$;
-- testers : RLS sans regle = illisible et non modifiable par anon/authenticated (service_role seulement).

-- Ajouter les testeurs (e-mails en minuscules), par exemple :
-- insert into public.testers (email) values ('prenom.nom@example.org') on conflict do nothing;
```

- [ ] **Step 2 : relecture.** Le script est idempotent : `if not exists` partout, `drop policy if exists` avant `create policy`, et `create or replace` pour la fonction. Aucune règle d'écriture n'est créée.

- [ ] **Step 3 : commit**

```bash
git add supabase/migrations/001_explorer.sql
git commit -m "feat(web): schema Supabase de l'explorateur (tables, RLS testeurs, is_tester)"
```

---

### Task 2 : lignes à synchroniser (`build_rows`)

**Fichiers :**
- à créer : `src/tsi_agent/supabase_sync.py`
- test : `tests/test_supabase_sync.py`

- [ ] **Step 1 : écrire les tests, qui doivent échouer**

```python
import json

import openpyxl

from tsi_agent.csr import COLUMNS
from tsi_agent.supabase_sync import TABLES, build_rows

PROJECT = """---
project_id: p1
title: Public administration reform
programme: TSI
year: 2024
countries:
- HR
policy_area: public-administration-and-governance
results: achieved
deliverables: 2
detail_page: true
source_urls:
- https://example.eu/p1
csrs:
- csr-HR-2022-1.1
---

# Public administration reform

Intro text.

## Summary (AI-generated)

The project reformed things.

## Sources

- https://example.eu/p1
"""

THIN = """---
project_id: p2
title: Thin project
programme: SRSP
year: 2019
countries:
- GR
detail_page: false
---

# Thin project
"""


def make_output(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "projects" / "p1.md").write_text(PROJECT, encoding="utf-8")
    (out / "projects" / "p2.md").write_text(THIN, encoding="utf-8")
    (out / "csr").mkdir()
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    for nbr, text in (("1", "Reform the administration. Cut red tape."), ("1.1", "Reform the administration."),
                      ("1.2", "Cut red tape.")):
        wb.active.append(["Croatia", "2022", nbr, "2023 - Semester Cycle (May)", text, "Public administration",
                          "-", "No", "Annual", "Some Progress"])
    wb.save(out / "csr" / "csr_full.xlsx")
    (out / "csr_links").mkdir()
    (out / "csr_links" / "p1.json").write_text(json.dumps({"project_id": "p1", "status": "ok", "basis": "full_text",
        "links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."},
                  {"csr_id": "csr-XX-2000-9.9", "level": "partial", "rationale": "unknown part"}]}), encoding="utf-8")
    (out / "csr_links" / "ghost.json").write_text(json.dumps({"project_id": "ghost", "status": "ok",
        "basis": "full_text", "links": [{"csr_id": "csr-HR-2022-1.2", "level": "partial", "rationale": "x"}]}),
        encoding="utf-8")
    return out


def test_build_rows_tables_and_projects(tmp_path):
    rows = build_rows(make_output(tmp_path), current_year=2026)
    assert list(rows) == list(TABLES) == ["projects", "csrs", "csr_parts", "project_csr_links", "anomalies"]
    p1, p2 = sorted(rows["projects"], key=lambda r: r["project_id"])
    assert p1 == {"project_id": "p1", "title": "Public administration reform", "programme": "TSI", "year": 2024,
                  "countries": ["HR"], "policy_area": "public-administration-and-governance", "results": "achieved",
                  "detail_page": True, "url": "https://example.eu/p1", "summary": "The project reformed things.",
                  "deliverables": 2, "csrs": ["csr-HR-2022-1.1"]}
    assert p2["summary"] is None and p2["csrs"] == [] and p2["deliverables"] == 0 and p2["url"] is None


def test_build_rows_csrs_parts_links_anomalies(tmp_path):
    rows = build_rows(make_output(tmp_path), current_year=2026)
    (csr,) = rows["csrs"]
    assert csr["csr_id"] == "csr-HR-2022-1" and csr["assessment"] == "Some Progress" and csr["assessment_year"] == 2023
    assert csr["policy_areas"] == ["Public administration"] and csr["title"] == "Croatia 2022 CSR 1"
    assert [p["part_id"] for p in rows["csr_parts"]] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]
    assert rows["csr_parts"][0]["csr_id"] == "csr-HR-2022-1"
    assert rows["project_csr_links"] == [{"project_id": "p1", "part_id": "csr-HR-2022-1.1", "level": "direct",
                                          "rationale": "R.", "basis": "full_text"}]
    types = {a["type"] for a in rows["anomalies"]}
    assert "srsp_title_only" in types
    assert all(set(a) == {"project_id", "title", "programme", "year", "countries", "type", "severity", "detail",
                          "url"} for a in rows["anomalies"])
    assert all(a["project_id"] is None or isinstance(a["project_id"], str) for a in rows["anomalies"])


def test_summary_falls_back_to_intro_and_is_capped(tmp_path):
    out = make_output(tmp_path)
    (out / "projects" / "p3.md").write_text(PROJECT.replace("p1", "p3").replace(
        "## Summary (AI-generated)\n\nThe project reformed things.\n\n", "").replace("Intro text.", "x" * 2000),
        encoding="utf-8")
    p3 = next(r for r in build_rows(out, current_year=2026)["projects"] if r["project_id"] == "p3")
    assert p3["summary"] == "x" * 1500
```

- [ ] **Step 2 : vérifier qu'ils échouent**

Run : `.venv/Scripts/python -m pytest tests/test_supabase_sync.py -q -p no:cacheprovider`
Expected : FAIL, `ModuleNotFoundError`.

- [ ] **Step 3 : implémenter le début de `src/tsi_agent/supabase_sync.py`**

```python
"""Synchronisation des donnees de l'explorateur web vers Supabase (PostgREST, cle service_role).

Remplacement complet : suppression des tables dans l'ordre des dependances, puis insertion par lots. La cle
service_role vient de l'environnement ou de `.env` (SUPABASE_URL, SUPABASE_SERVICE_KEY) : jamais d'un fichier
suivi par git. La table `testers` n'est jamais touchee.
"""

import json
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from tsi_agent.anomalies import detect, load_csr_links, load_projects, load_publications
from tsi_agent.chunking import INTRO, SUMMARY, split_sections
from tsi_agent.csr import build_csrs, read_rows
from tsi_agent.render import parse_front_matter

TABLES = ("projects", "csrs", "csr_parts", "project_csr_links", "anomalies")
# Suppression : liens avant les tables qu'ils referencent ; insertion dans l'ordre inverse.
DELETE_ORDER = ("project_csr_links", "csr_parts", "csrs", "projects", "anomalies")
INSERT_ORDER = ("projects", "csrs", "csr_parts", "project_csr_links", "anomalies")
# Filtre PostgREST couvrant toutes les lignes (DELETE sans filtre refuse) : colonne non nulle de chaque table.
DELETE_FILTER = {"projects": "project_id=not.is.null", "csrs": "csr_id=not.is.null",
                 "csr_parts": "part_id=not.is.null", "project_csr_links": "project_id=not.is.null",
                 "anomalies": "id=not.is.null"}
BATCH = 500
SUMMARY_CHARS = 1500


def _project_row(fm: dict, body: str) -> dict[str, Any]:
    sections = dict(split_sections(body))
    summary = sections.get(SUMMARY) or sections.get(INTRO) or None
    urls = fm.get("source_urls") or []
    csrs = fm.get("csrs")
    return {"project_id": fm["project_id"], "title": str(fm.get("title") or fm["project_id"]),
            "programme": fm.get("programme"), "year": fm.get("year") if isinstance(fm.get("year"), int) else None,
            "countries": list(fm.get("countries") or []), "policy_area": fm.get("policy_area"),
            "results": fm.get("results"), "detail_page": bool(fm.get("detail_page")),
            "url": urls[0] if urls else None, "summary": summary[:SUMMARY_CHARS] if summary else None,
            "deliverables": int(fm.get("deliverables") or 0), "csrs": list(csrs) if isinstance(csrs, list) else []}


def build_rows(output_dir: Path, current_year: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Lignes de chaque table, depuis output/ (sans reseau)."""
    projects = []
    for path in sorted((output_dir / "projects").glob("*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except ValueError:
            continue
        if isinstance(fm, dict) and fm.get("project_id"):
            projects.append(_project_row(fm, body))
    csr_rows, part_rows = [], []
    xlsx = output_dir / "csr" / "csr_full.xlsx"
    for csr in build_csrs(read_rows(xlsx)) if xlsx.exists() else []:
        latest = csr.latest_assessment
        csr_rows.append({"csr_id": csr.csr_id, "country": csr.country, "year": csr.year, "number": csr.number,
                         "title": csr.title, "text": csr.text, "policy_areas": csr.policy_areas,
                         "assessment": latest[1] if latest else None, "assessment_year": latest[0] if latest else None,
                         "source_url": "https://ec.europa.eu/economy_finance/country-specific-recommendations-database/"})
        for part in csr.subparts or ([csr.full] if csr.full else []):
            part_rows.append({"part_id": part.part_id, "csr_id": csr.csr_id, "country": csr.country,
                              "year": csr.year, "text": part.text, "policy_areas": part.policy_areas,
                              "assessment": part.assessment})
    project_ids = {p["project_id"] for p in projects}
    part_ids = {p["part_id"] for p in part_rows}
    links = []
    for path in sorted((output_dir / "csr_links").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("status") != "ok" or record.get("project_id") not in project_ids:
            continue
        for link in record.get("links") or []:
            if link.get("csr_id") in part_ids:
                links.append({"project_id": record["project_id"], "part_id": link["csr_id"],
                              "level": link.get("level"), "rationale": link.get("rationale") or "",
                              "basis": record.get("basis") or "full_text"})
    anomalies = [{**asdict(a), "project_id": a.project_id or None}
                 for a in detect(load_projects(output_dir), load_publications(output_dir),
                                 current_year or date.today().year, csr_links=load_csr_links(output_dir))]
    return {"projects": projects, "csrs": csr_rows, "csr_parts": part_rows, "project_csr_links": links,
            "anomalies": anomalies}
```

Vérifier les points suivants dans `csr.py` :
- `Csr.latest_assessment` renvoie `(année, évaluation)` ;
- `Part` a `part_id`, `text`, `policy_areas` et `assessment`.

S'ils ont changé, adapter le code.

`anomalies.detect` renvoie des `Anomaly` dont les champs correspondent aux colonnes de la table `anomalies`, hors `id`.

- [ ] **Step 4 : vérifier qu'ils passent**

Run : `.venv/Scripts/python -m pytest tests/test_supabase_sync.py -q -p no:cacheprovider`
Expected : 3 tests passent.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/supabase_sync.py tests/test_supabase_sync.py
git commit -m "feat(web): lignes de l'explorateur depuis output/ (projets, CSR, sous-parties, liens, anomalies)"
```

---

### Task 3 : envoi vers Supabase, identifiants et commande `supabase-sync`

**Fichiers :**
- à modifier : `src/tsi_agent/supabase_sync.py`, `src/tsi_agent/__main__.py`
- tests : `tests/test_supabase_sync.py`, `tests/test_cli.py`

- [ ] **Step 1 : écrire les tests**

```python
import httpx
import pytest

from tsi_agent.supabase_sync import SyncError, credentials, push


def recorder(fail_on=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.query.decode(), request.headers.get("apikey"),
                      request.headers.get("authorization"), len(json.loads(request.content)) if request.content else 0))
        if fail_on and fail_on == (request.method, request.url.path):
            return httpx.Response(400, json={"message": "bad"})
        return httpx.Response(204 if request.method == "DELETE" else 201)

    return calls, httpx.Client(transport=httpx.MockTransport(handler))


def test_push_deletes_then_inserts_in_batches(tmp_path):
    rows = {"projects": [{"project_id": f"p{i}"} for i in range(1201)], "csrs": [], "csr_parts": [],
            "project_csr_links": [{"project_id": "p1", "part_id": "x"}], "anomalies": [{"type": "t"}]}
    calls, client = recorder()
    summary = push(rows, "https://abc.supabase.co", "service-key", client=client, backup=tmp_path / "rows.json")
    methods = [(m, p.removeprefix("/rest/v1/")) for m, p, *_ in calls]
    assert methods[:5] == [("DELETE", "project_csr_links"), ("DELETE", "csr_parts"), ("DELETE", "csrs"),
                           ("DELETE", "projects"), ("DELETE", "anomalies")]
    assert methods[5:] == [("POST", "projects")] * 3 + [("POST", "project_csr_links"), ("POST", "anomalies")]
    assert [c[5] for c in calls[5:8]] == [500, 500, 201]
    assert calls[0][2] == "project_id=not.is.null"
    assert all(c[3] == "service-key" and c[4] == "Bearer service-key" for c in calls)
    assert summary == {"projects": 1201, "csrs": 0, "csr_parts": 0, "project_csr_links": 1, "anomalies": 1}
    assert json.loads((tmp_path / "rows.json").read_text(encoding="utf-8"))["projects"][0] == {"project_id": "p0"}


def test_push_http_error_is_a_sync_error(tmp_path):
    calls, client = recorder(fail_on=("POST", "/rest/v1/projects"))
    with pytest.raises(SyncError, match="projects.*400.*bad"):
        push({"projects": [{"project_id": "p"}], "csrs": [], "csr_parts": [], "project_csr_links": [],
              "anomalies": []}, "https://abc.supabase.co", "k", client=client, backup=tmp_path / "r.json")


def test_credentials_from_env_or_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SyncError, match="SUPABASE_SERVICE_KEY"):
        credentials(tmp_path / ".env")
    (tmp_path / ".env").write_text("# c\nSUPABASE_URL=https://abc.supabase.co/\nSUPABASE_SERVICE_KEY='k1'\n",
                                   encoding="utf-8")
    assert credentials(tmp_path / ".env") == ("https://abc.supabase.co", "k1")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k2")
    assert credentials(tmp_path / ".env") == ("https://abc.supabase.co", "k2")
```

Ajouter dans `tests/test_cli.py` :

```python
def test_supabase_sync_without_credentials_is_a_clean_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    sources = tmp_path / "sources.yaml"
    sources.write_text("{}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "--sources", str(sources), "supabase-sync"]) == 1
    assert "SUPABASE_SERVICE_KEY" in capsys.readouterr().err
```

- [ ] **Step 2 : vérifier qu'ils échouent**

Run : `.venv/Scripts/python -m pytest tests/test_supabase_sync.py tests/test_cli.py -q -p no:cacheprovider`
Expected : FAIL, `ImportError` sur `SyncError`.

- [ ] **Step 3 : implémenter**

Ajouter dans `supabase_sync.py` :

```python
class SyncError(RuntimeError):
    pass


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


def credentials(dotenv: Path = Path(".env")) -> tuple[str, str]:
    """(url, cle service_role) : variables d'environnement, sinon `.env` ; erreur claire si absentes."""
    file = _dotenv(dotenv)
    url = os.environ.get("SUPABASE_URL") or file.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or file.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SyncError("SUPABASE_URL et SUPABASE_SERVICE_KEY requis (environnement ou .env, jamais dans git)")
    return url.rstrip("/"), key


def _check(resp: httpx.Response, table: str, action: str) -> None:
    if resp.status_code >= 300:
        raise SyncError(f"{action} {table}: HTTP {resp.status_code} {resp.text[:500]}")


def push(rows: dict[str, list[dict[str, Any]]], url: str, key: str, *, client: httpx.Client | None = None,
         backup: Path | None = None) -> dict[str, int]:
    """Remplace le contenu des tables (sauf testers) ; sauvegarde JSON des lignes avant toute suppression."""
    if backup is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Prefer": "return=minimal"}
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        for table in DELETE_ORDER:
            _check(client.delete(f"{url}/rest/v1/{table}?{DELETE_FILTER[table]}", headers=headers), table, "delete")
        for table in INSERT_ORDER:
            data = rows.get(table) or []
            for start in range(0, len(data), BATCH):
                _check(client.post(f"{url}/rest/v1/{table}", headers=headers,
                                   content=json.dumps(data[start:start + BATCH], ensure_ascii=False)), table, "insert")
    finally:
        if own:
            client.close()
    return {table: len(rows.get(table) or []) for table in INSERT_ORDER}
```

Dans `__main__.py`, ajouter le parseur après `csr-link` :

```python
    sub.add_parser("supabase-sync", help="publier projets, CSR, liens et anomalies dans Supabase (explorateur web)")
```

puis le bloc :

```python
    if args.command == "supabase-sync":
        from tsi_agent.supabase_sync import SyncError, build_rows, credentials, push
        try:
            url, key = credentials()
            rows = build_rows(Path(settings.output_dir))
            summary = push(rows, url, key, backup=Path(settings.output_dir) / "state" / "supabase_rows.json")
        except (SyncError, httpx.HTTPError) as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2))
        return 0
```

Ajouter `import httpx` dans ce bloc, ou en tête du fichier si `httpx` n'y est pas encore importé. Placer le bloc **avant** tout code qui exige le fichier `sources` ; sinon, garder `--sources` comme dans le test.

- [ ] **Step 4 : vérifier qu'ils passent**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`
Expected : toute la suite passe.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/supabase_sync.py src/tsi_agent/__main__.py tests/test_supabase_sync.py tests/test_cli.py
git commit -m "feat(web): commande supabase-sync (remplacement complet, sauvegarde, cle service_role hors git)"
```

---

### Task 4 : la page web (`web/`)

**Fichiers :** à créer : `web/index.html`, `web/style.css`, `web/app.js`, `web/config.js`

Pas de build, pas de framework. `supabase-js` est chargé en module ES :

```js
import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/+esm";
```

- [ ] **Step 1 : `web/config.js`**

```js
// Configuration publique : la cle anon est faite pour etre publiee (lecture protegee par RLS, testeurs seulement).
// NE JAMAIS mettre ici la cle service_role.
export const SUPABASE_URL = "https://vxwmhsgxoomcbiukeinp.supabase.co";
export const SUPABASE_ANON_KEY = "REMPLACER_PAR_LA_CLE_ANON";
```

L'utilisateur fournira la clé `anon`, que le contrôleur reportera dans le fichier.

- [ ] **Step 2 : `web/index.html`**

- `<!doctype html>`, `lang="en"`, metas `charset` et `viewport`, titre « TSI × CSR Explorer ».
- `<link rel="stylesheet" href="style.css">` et `<script type="module" src="app.js">`.
- Structure :
  - en-tête : titre, sous-titre « DG REFORM technical support projects and EU country-specific recommendations », zone d'état de connexion et bouton de déconnexion ;
  - `<section id="login">` : champ e-mail, bouton « Send magic link », zone de message ;
  - `<section id="denied">` : « Access not authorised for this e-mail » ;
  - `<nav>` : onglets `#projects`, `#csrs`, `#anomalies` ;
  - `<main id="view">` ;
  - pied de page : « AI-generated summaries and project–CSR links: verify before use. Sources: reforms-investments.ec.europa.eu, ECFIN CSR database. ».
- Textes de l'interface en anglais, la langue du corpus et de la DG.

- [ ] **Step 3 : `web/style.css`**

- Tokens de couleur sur `:root` : `--bg`, `--fg`, `--muted`, `--accent` (bleu EU `#004494`), `--card`, `--border`, `--direct` (vert), `--partial` (ambre), et les couleurs de gravité `high`, `medium`, `low` et `info`.
- Thème sombre avec `@media (prefers-color-scheme: dark)`. Fond explicite sur `body`.
- Police système. Mise en page sur une colonne, largeur maximale de 1 100 px, marges latérales de 16 px. Grille de filtres avec `flex-wrap`. Tableaux qui défilent horizontalement dans leur conteneur, sans défilement horizontal de la page. Badges pour le niveau et la gravité.

- [ ] **Step 4 : `web/app.js`**

**Fonctionnalités :**

1. **Auth**
   - `const sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)`.
   - Au chargement, `sb.auth.getSession()`. Sans session, on affiche `#login`.
   - La connexion se fait avec `sb.auth.signInWithOtp({ email, options: { emailRedirectTo: location.origin + location.pathname } })`, puis le message « Check your mailbox ».
   - Avec une session, appeler `sb.rpc("is_tester")`. S'il renvoie `false`, afficher `#denied` et le bouton de déconnexion. Sinon, afficher la navigation et router.
   - `sb.auth.onAuthStateChange` relance le rendu.
2. **Routage par le hash**
   - `#projects?q=..&country=..&programme=..&from=..&to=..&area=..&linked=1&detail=1&page=N`
   - `#project/<id>`
   - `#csrs?country=..&year=..&q=..&page=N`
   - `#csr/<csr_id>`
   - `#anomalies?type=..&severity=..&programme=..&from=..&to=..&country=..&page=N`
   - `window.addEventListener("hashchange", render)`.
   - Les filtres modifient le hash, jamais un état caché.
3. **Liste des projets**

   ```js
   let q = sb.from("projects").select("project_id,title,programme,year,countries,policy_area,detail_page,csrs", { count: "exact" })
     .order("year", { ascending: false }).order("title").range(page * 50, page * 50 + 49);
   ```

   Filtres :
   - `ilike("title", "%q%")` (échapper `%` et `,`) ;
   - `contains("countries", [country])` ;
   - `eq("programme", ...)` ;
   - `gte` et `lte` sur `year` ;
   - `eq("policy_area", ...)` ;
   - `linked` → `not("csrs", "eq", "{}")` ;
   - `detail` → `eq("detail_page", true)`.

   Le tableau affiche le titre (lien vers `#project/id`), le programme, l'année, les pays, le domaine et le nombre de CSR liées. Il est suivi de la pagination et du total.

   Les listes déroulantes des filtres sont :
   - pays : codes ISO de l'UE-27, en dur dans `app.js`, avec leur nom ;
   - programmes : TSI, SRSP, SRSS ;
   - domaines : les 10 slugs de `config/sources.yaml`, en dur.
4. **Fiche projet**
   - Requête : `sb.from("projects").select("*").eq("project_id", id).single()`.
   - Puis :

     ```js
     sb.from("project_csr_links").select("level,rationale,basis,part_id,csr_parts(text,assessment,csr_id,csrs(title,year))").eq("project_id", id)
     ```

   - Affichage :
     - les faits ;
     - un lien « Project page » vers `url` ;
     - le résumé, sous le titre « AI-generated summary » ;
     - la section « Supported CSRs (AI-assessed) ». Chaque lien y montre : un badge de niveau, `part_id` (lien vers `#csr/<csr_id>`), le titre de la CSR, le texte de la sous-partie, l'évaluation et la justification. Si `basis = title_only`, ajouter la mention « based on the project title only ».
     - S'il n'y a aucun lien : « No CSR link assessed ».
5. **Liste des CSR**
   - Requête :

     ```js
     sb.from("csrs").select("csr_id,country,year,number,title,assessment,assessment_year", { count: "exact" })
     ```

   - Filtres : pays, année, `q` (avec `ilike` sur `text`).
   - Tri par année décroissante, puis pays et numéro (numérique, ou tri côté client par page). 50 lignes par page.
6. **Fiche CSR**
   - Requêtes : `sb.from("csrs").select("*").eq("csr_id", id).single()`, puis :

     ```js
     sb.from("csr_parts").select("part_id,text,policy_areas,assessment,project_csr_links(level,rationale,projects(project_id,title,programme,year))").eq("csr_id", id).order("part_id")
     ```

   - Affichage : le texte, les domaines, puis « Latest assessment (year): X ». Pour chaque sous-partie : son texte, son évaluation et les projets liés (lien vers `#project/id`, badge de niveau, justification).
   - En tête : « N projects support this CSR (AI-assessed) ».
7. **Anomalies**
   - Requête : `sb.from("anomalies").select("*", { count: "exact" })`, avec les filtres type, gravité, programme, plage d'années et pays (`ilike("countries", "%XX%")`).
   - Tri par gravité, dans l'ordre high, medium, low, info. Côté serveur, utiliser `order("severity")` puis `order("type")`, en documentant que l'ordre est alphabétique, ou filtrer par défaut sur `severity in (high, medium)`. Le choix est laissé à l'implémenteur, mais il doit être cohérent.
   - Tableau : titre (lien vers `#project/id` s'il existe, sinon l'URL), type, gravité (badge), programme, année, pays, détail.
   - Bouton « Download CSV » : refait la requête avec `range(0, 4999)`, puis génère un CSV (BOM UTF-8, champs entre guillemets) et le télécharge avec `Blob` et `URL.createObjectURL`.
8. **Erreurs**
   - Toute erreur Supabase affiche un message lisible dans la vue, sans rien dans la console seulement.
   - Pendant les requêtes, afficher l'état « Loading… ».
9. **Sécurité de l'affichage**
   - Tout texte venant de la base passe par une fonction `esc()`, qui échappe le HTML, avant d'être injecté.
   - Les URL ne sont acceptées que si elles commencent par `https://`.

**Organisation :**
- fonctions courtes par vue : `renderProjects`, `renderProject`, `renderCsrs`, `renderCsr`, `renderAnomalies` ;
- helpers : `esc`, `params()`, `setParams()`, `table()`, `badge()`, `pager()`, `countriesSelect()`.

- [ ] **Step 5 : vérification locale**
  - Lancer : `.venv/Scripts/python -m http.server 8000 -d web`.
  - Ouvrir `http://localhost:8000` : sans clé `anon` valide, la page doit afficher l'écran de connexion sans erreur JavaScript bloquante.
  - Contrôler la syntaxe avec `node --check web/app.js`, si Node est installé.

- [ ] **Step 6 : commit**

```bash
git add web/
git commit -m "feat(web): explorateur statique (projets, CSR et lignage, anomalies) avec connexion Supabase"
```

---

### Task 5 : déploiement GitHub Pages et documentation

**Fichiers :**
- à créer : `.github/workflows/pages.yml`
- à modifier : `README.md`

- [ ] **Step 1 : workflow**

```yaml
name: Deploy explorer to GitHub Pages
on:
  push:
    branches: [main]
    paths: ["web/**", ".github/workflows/pages.yml"]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
concurrency:
  group: pages
  cancel-in-progress: true
jobs:
  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: web
      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 2 : section README « Explorateur web (Supabase + GitHub Pages) »**

Elle doit couvrir :
- l'URL de la page ;
- le schéma : coller `supabase/migrations/001_explorer.sql` dans le *SQL Editor* ;
- l'ajout de testeurs : `insert into public.testers ...` ;
- *Auth > URL Configuration* : *Site URL* et *Redirect URLs* = l'URL de la page ;
- `.env` : `SUPABASE_URL` et `SUPABASE_SERVICE_KEY`, jamais commités ;
- `python -m tsi_agent supabase-sync` après chaque mise à jour du corpus ;
- *Settings > Pages* : source « GitHub Actions » ;
- la liste de contrôle :
  - connexion d'un testeur ;
  - un e-mail non autorisé voit « Access not authorised » ;
  - un appel REST anonyme sur `/rest/v1/projects` renvoie `[]`.

- [ ] **Step 3 : commit**

```bash
git add .github/workflows/pages.yml README.md
git commit -m "ci(web): deploiement GitHub Pages de web/ ; documentation de l'explorateur"
```

---

### Task 6 : mise en service (contrôleur et utilisateur)

1. **Utilisateur, dans Supabase :**
   - exécuter le SQL ;
   - ajouter son e-mail dans `testers` ;
   - *Auth > URL Configuration* : `https://mge-agilos.github.io/dg-reform-tsi/` ;
   - mettre `SUPABASE_URL` et `SUPABASE_SERVICE_KEY` dans `.env` ;
   - transmettre la clé `anon`.
2. **Contrôleur :**
   - reporter la clé `anon` dans `web/config.js` et commiter ;
   - lancer `supabase-sync` ;
   - vérifier qu'un `GET /rest/v1/projects?limit=1` anonyme renvoie `[]`.
3. **Utilisateur :**
   - pousser `feature/web-explorer`, puis la fusionner dans `main` et pousser `main`. Commandes en PowerShell 5.1 :

     ```powershell
     git push -u origin feature/web-explorer; if ($?) { git checkout main }; if ($?) { git merge feature/web-explorer }; if ($?) { git push }
     ```

   - *Settings > Pages* : source « GitHub Actions » ;
   - attendre le déploiement, puis tester la connexion.
