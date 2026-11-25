# On-premises monthly update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The monthly reload of the self-hosted installation (Podman/Docker, external PostgreSQL) fetches new projects, publications, CSRs, recitals and country reports for the current Semester year and loads them into the database in one transaction.

**Architecture:** New module `tsi_agent/db_sync.py` (psycopg 3, same rows as `supabase-sync`), year detection in `csr.py`, `csr_commission.py`, `country_reports.py` and `web/app.js`, a bash `deploy/onprem/update.sh` mirroring `deploy/updater/update.ps1`, an updater image and compose file in `deploy/onprem/docker/`.

**Tech Stack:** Python 3.12, psycopg 3.2 (binary), pytest; bash; Node test runner for `web/`; PostgreSQL 16 bench.

Spec: `docs/superpowers/specs/2026-11-25-onprem-monthly-update-design.md`.

**Constraints learnt from the neighbouring sessions (25 Nov 2026):**
- Client: Podman + podman-compose (`~/tsi-docker`), external PostgreSQL `tsi_explorer`, schema `tsi_explorer`,
  `sslmode=require`; PostgREST logs in as `app_mngt_u` (not `tsi_authenticator`); `grant service_role to app_mngt_u`
  requested from the DBA, not confirmed; `service_role` has no BYPASSRLS (900 rules); DBA has no psql; HTTP proxy
  on the host (`NO_PROXY` for internal hosts); `$` in `.env` values interpolated by podman-compose in compose files.
- Bench: `pg_ctl -D <bc90fabb scratchpad>/pg/data -l <...>/pg/pg.log -o "-p 55432" start -w`, `PGCLIENTENCODING=UTF8`,
  users postgres / tsi_owner / tsi_authenticator (password `test`); databases `tsi_explorer` (public) and
  `tsi_schema_test` (schema `tsi_explorer`, 900 rules). `service_role` has BYPASSRLS on the bench: remove it for the
  client case test, restore afterwards. Stop the bench when done. Data holds real e-mail addresses: local only.
- The assistant prompts say "2026" (owned by the assistant session): not changed here.

---

## File structure

| File | Responsibility |
|---|---|
| `src/tsi_agent/db_sync.py` (new) | Settings, transactional load, dry-run check |
| `src/tsi_agent/__main__.py` | `db-sync` command; `csr-build` / `csr-link` / `csr-related` use detected years |
| `src/tsi_agent/csr.py` | `build_csrs(year_to=None)` = latest year of the export |
| `src/tsi_agent/csr_commission.py` | Per-year publication ids, source years, all-year loaders |
| `src/tsi_agent/supabase_sync.py` | Recitals of every Commission year |
| `src/tsi_agent/country_reports.py` | Year fallback when fetching, latest year per country when loading |
| `web/app.js` | Recitals / country reports not gated on 2026; labels from the data |
| `deploy/onprem/update.sh` (new) | Linux update script |
| `deploy/onprem/docker/updater.Dockerfile`, `compose.updater.yaml`, `updater.env.example` (new) | Updater container |
| `deploy/onprem/README.md`, `README.md` | Documentation |
| `pyproject.toml` | `psycopg[binary]>=3.2` |
| Tests | `tests/test_db_sync.py`, `tests/test_onprem_update_sh.py` (new); `tests/test_csr.py`, `tests/test_csr_commission.py`, `tests/test_country_reports.py`, `tests/js/app-load.test.mjs` |

---

### Task 1: dependency

**Files:** Modify `pyproject.toml`

- [ ] **Step 1:** add `"psycopg[binary]>=3.2",` after `"websockets>=14",` in `dependencies`.
- [ ] **Step 2:** `.venv\Scripts\python -m pip install -e ".[dev]"`; `python -c "import psycopg; print(psycopg.__version__)"` → 3.2 or later.
- [ ] **Step 3:** commit `build: psycopg for the direct PostgreSQL load`.

### Task 2: `db_sync` settings

**Files:** Create `src/tsi_agent/db_sync.py`; Test `tests/test_db_sync.py`

- [ ] **Step 1: failing tests**

```python
import pytest

from tsi_agent.db_sync import database_settings
from tsi_agent.supabase_sync import SyncError

URL = "postgresql://u:p@db:5432/tsi_explorer?sslmode=require"


def test_settings_from_env_then_dotenv(tmp_path, monkeypatch):
    for k in ("TSI_DATABASE_URL", "TSI_DB_SCHEMA", "TSI_DB_ROLE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"TSI_DATABASE_URL={URL}\nTSI_DB_SCHEMA=tsi_explorer\nTSI_DB_ROLE=service_role\n", encoding="utf-8")
    assert database_settings(env) == (URL, "tsi_explorer", "service_role")
    monkeypatch.setenv("TSI_DB_SCHEMA", "other")
    assert database_settings(env)[1] == "other"


def test_settings_defaults_and_errors(tmp_path, monkeypatch):
    for k in ("TSI_DATABASE_URL", "TSI_DB_SCHEMA", "TSI_DB_ROLE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SyncError, match="TSI_DATABASE_URL"):
        database_settings(tmp_path / ".env")
    monkeypatch.setenv("TSI_DATABASE_URL", URL)
    assert database_settings(tmp_path / ".env") == (URL, "public", "")
    monkeypatch.setenv("TSI_DB_SCHEMA", "Bad-Name")
    with pytest.raises(SyncError, match="TSI_DB_SCHEMA"):
        database_settings(tmp_path / ".env")
    monkeypatch.setenv("TSI_DB_SCHEMA", "public")
    monkeypatch.setenv("TSI_DB_ROLE", "x; drop")
    with pytest.raises(SyncError, match="TSI_DB_ROLE"):
        database_settings(tmp_path / ".env")


def test_settings_error_never_prints_the_url(tmp_path, monkeypatch):
    monkeypatch.setenv("TSI_DATABASE_URL", URL)
    monkeypatch.setenv("TSI_DB_SCHEMA", "Bad")
    with pytest.raises(SyncError) as exc:
        database_settings(tmp_path / ".env")
    assert "p@db" not in str(exc.value)
```

- [ ] **Step 2:** `pytest tests/test_db_sync.py -q` → ImportError.
- [ ] **Step 3: implementation** (module start; Task 3 adds the load)

```python
"""Chargement des donnees de l'explorateur dans la base PostgreSQL d'une installation auto-hebergee (db-sync).

Connexion directe (psycopg 3), UNE transaction : en cas d'erreur, la base garde son contenu precedent. Memes lignes
que supabase-sync (`build_rows`). Reglages : TSI_DATABASE_URL (URL libpq, jamais affichee : elle contient le mot de
passe), TSI_DB_SCHEMA (defaut public), TSI_DB_ROLE (optionnel : `set local role`, par exemple service_role, qui ecrit
malgre la securite par ligne grace aux regles de deploy/onprem/sql/900_service_role.sql). Environnement, sinon `.env`.
Jamais touchees : testers, link_reviews, page_views, user_passwords, et les analyses d'impact lancees depuis la page.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tsi_agent.llm import read_dotenv
from tsi_agent.supabase_sync import (DELETE_ORDER, IMPACT_TABLE, INSERT_ORDER, OPTIONAL_TABLES, PROTECTED_TABLES,
                                     SyncError)

BATCH = 500
NEVER_WRITTEN = (*PROTECTED_TABLES, "page_views", "user_passwords")
_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def database_settings(dotenv: Path = Path(".env")) -> tuple[str, str, str]:
    """(url, schema, role) : environnement, sinon `.env` ; SyncError claire (sans l'URL) si invalide."""
    file = read_dotenv(dotenv)

    def get(key: str) -> str:
        return (os.environ.get(key) or file.get(key) or "").strip()

    url, schema, role = get("TSI_DATABASE_URL"), get("TSI_DB_SCHEMA") or "public", get("TSI_DB_ROLE")
    if not url:
        raise SyncError(f"TSI_DATABASE_URL requis (environnement ou {dotenv.resolve()}, jamais dans git)")
    if not _NAME.fullmatch(schema):
        raise SyncError("TSI_DB_SCHEMA : lettres minuscules, chiffres et _ seulement")
    if role and not _NAME.fullmatch(role):
        raise SyncError("TSI_DB_ROLE : lettres minuscules, chiffres et _ seulement")
    return url, schema, role
```

- [ ] **Step 4:** tests pass. **Step 5:** commit `feat(db-sync): connection settings`.

### Task 3: `db_sync.load` (one transaction)

**Files:** Modify `src/tsi_agent/db_sync.py`; Test `tests/test_db_sync.py`

- [ ] **Step 1: failing tests** (fake connection recording rendered statements)

```python
import json as _json

from tsi_agent.db_sync import BATCH, load


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        text = stmt if isinstance(stmt, str) else stmt.as_string()
        self.conn.log.append((text, params))
        if self.conn.fail_on and self.conn.fail_on in text:
            raise RuntimeError("boom")
        if text.startswith("select to_regclass"):
            self._one = (None if params[0].split(".")[1] in self.conn.missing else params[0],)

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, missing=(), fail_on=None):
        self.log, self.missing, self.fail_on = [], set(missing), fail_on
        self.commits = self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def rows_fixture(n_projects=3):
    return {"projects": [{"project_id": f"p{i}", "title": "T", "countries": ["HR"]} for i in range(n_projects)],
            "csr_documents": [], "csrs": [], "csr_parts": [], "project_csr_links": [],
            "anomalies": [{"project_id": None, "type": "x", "severity": "low"}],
            "impact_reports": [{"project_id": "p0", "score": 5, "report": "r", "source": "local"}],
            "csr_recitals": [{"recital_id": "rec-HR-2026-1"}], "country_reports": [],
            "country_report_passages": [], "csr_related_projects": []}


def statements(conn):
    return [s for s, _ in conn.log]


def test_load_deletes_then_inserts_in_one_transaction_with_role():
    conn = FakeConn()
    summary = load(rows_fixture(), "postgresql://x", "tsi_explorer", "service_role", connect=lambda url: conn)
    s = statements(conn)
    assert s[0] == 'set local role "service_role"'
    deletes = [x for x in s if x.startswith("delete from")]
    assert deletes[:6] == [f'delete from "tsi_explorer"."{t}"' for t in
                           ("project_csr_links", "csr_parts", "csrs", "csr_documents", "projects", "anomalies")]
    assert 'delete from "tsi_explorer"."impact_reports" where source = \'local\'' in s
    first_insert = next(x for x in s if x.startswith("insert into"))
    assert first_insert.startswith('insert into "tsi_explorer"."projects" ("project_id", "title", "countries")')
    assert 'json_populate_recordset(null::"tsi_explorer"."projects", %s::json)' in first_insert
    assert conn.commits >= 1 and conn.rollbacks == 0 and conn.closed
    assert summary["projects"] == 3 and summary["impact_reports"] == 1 and summary["csr_recitals"] == 1
    assert not any("testers" in x or "link_reviews" in x or "page_views" in x or "user_passwords" in x for x in s)


def test_load_batches_and_json_parameter():
    conn = FakeConn()
    load(rows_fixture(BATCH + 1), "u", "public", "", connect=lambda url: conn)
    inserts = [(x, p) for x, p in conn.log if x.startswith('insert into "public"."projects"')]
    assert len(inserts) == 2
    assert len(_json.loads(inserts[0][1][0])) == BATCH and len(_json.loads(inserts[1][1][0])) == 1
    assert not any(x.startswith("set local role") for x in statements(conn))


def test_load_skips_missing_optional_table_with_warning():
    conn = FakeConn(missing={"csr_recitals"})
    summary = load(rows_fixture(), "u", "public", "", connect=lambda url: conn)
    assert summary["csr_recitals"] == 0
    assert any("csr_recitals" in w and "009_csr_recitals.sql" in w for w in summary["warnings"])
    assert not any(x.startswith('delete from "public"."csr_recitals"') for x in statements(conn))


def test_load_rolls_back_on_error():
    conn = FakeConn(fail_on='insert into "public"."anomalies"')
    with pytest.raises(SyncError, match="insert anomalies"):
        load(rows_fixture(), "u", "public", "", connect=lambda url: conn)
    assert conn.rollbacks == 1 and conn.commits == 0 and conn.closed


def test_load_refuses_protected_tables():
    rows = rows_fixture()
    rows["testers"] = [{"email": "a@b"}]
    with pytest.raises(SyncError, match="protegees"):
        load(rows, "u", "public", "", connect=lambda url: FakeConn())


def test_load_writes_backup_before_connecting(tmp_path):
    def refuse(url):
        raise RuntimeError("no db")
    backup = tmp_path / "state" / "db_rows.json"
    with pytest.raises(SyncError):
        load(rows_fixture(), "u", "public", "", connect=refuse, backup=backup)
    assert _json.loads(backup.read_text(encoding="utf-8"))["projects"][0]["project_id"] == "p0"
```

- [ ] **Step 2:** run → `load` not defined.
- [ ] **Step 3: implementation** (append to `db_sync.py`)

```python
def _table(schema: str, table: str):
    from psycopg import sql
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _insert(cur: Any, schema: str, table: str, data: list[dict[str, Any]]) -> None:
    from psycopg import sql
    if not data:
        return
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in data[0])
    stmt = sql.SQL("insert into {t} ({c}) select {c} from json_populate_recordset(null::{t}, %s::json)").format(
        t=_table(schema, table), c=cols)
    for start in range(0, len(data), BATCH):
        cur.execute(stmt, (json.dumps(data[start:start + BATCH], ensure_ascii=False),))


def _exists(cur: Any, schema: str, table: str) -> bool:
    cur.execute("select to_regclass(%s)", (f"{schema}.{table}",))
    return cur.fetchone()[0] is not None


def _connect(url: str) -> Any:
    import psycopg
    return psycopg.connect(url)


def load(rows: dict[str, list[dict[str, Any]]], url: str, schema: str, role: str = "", *,
         connect: Callable[[str], Any] = _connect, backup: Path | None = None) -> dict[str, Any]:
    """Remplace les donnees publiques dans UNE transaction ; resume {table: lignes, warnings}. SyncError sinon."""
    from psycopg import sql
    written = {*DELETE_ORDER, *INSERT_ORDER, *(t for t in (IMPACT_TABLE, *(o[0] for o in OPTIONAL_TABLES)) if t in rows)}
    if written & set(NEVER_WRITTEN) or set(rows) & set(NEVER_WRITTEN):
        raise SyncError(f"tables protegees dans la synchronisation : {sorted((written | set(rows)) & set(NEVER_WRITTEN))}")
    if backup is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    summary: dict[str, Any] = {}
    warnings: list[str] = []
    step = ("connect", "database")
    try:
        conn = connect(url)
    except Exception as exc:
        raise SyncError(f"connexion a la base impossible : {type(exc).__name__}: {exc}") from exc
    done: list[str] = []
    try:
        with conn.cursor() as cur:
            if role:
                step = ("set role", role)
                cur.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            for table in DELETE_ORDER:
                step = ("delete", table)
                cur.execute(sql.SQL("delete from {}").format(_table(schema, table)))
            for table in INSERT_ORDER:
                step = ("insert", table)
                _insert(cur, schema, table, rows.get(table) or [])
                summary[table] = len(rows.get(table) or [])
            done = [*DELETE_ORDER]
            if IMPACT_TABLE in rows:
                step = ("delete", IMPACT_TABLE)
                cur.execute(sql.SQL("delete from {} where source = 'local'").format(_table(schema, IMPACT_TABLE)))
                step = ("insert", IMPACT_TABLE)
                _insert(cur, schema, IMPACT_TABLE, rows.get(IMPACT_TABLE) or [])
                summary[IMPACT_TABLE] = len(rows.get(IMPACT_TABLE) or [])
                done.append(IMPACT_TABLE)
            for table, _, migration in OPTIONAL_TABLES:
                if table not in rows:
                    continue
                step = ("check", table)
                if not _exists(cur, schema, table):
                    warnings.append(f"table {table} absente (appliquer supabase/migrations/{migration}) : non synchronisee")
                    summary[table] = 0
                    continue
                step = ("delete", table)
                cur.execute(sql.SQL("delete from {}").format(_table(schema, table)))
                step = ("insert", table)
                _insert(cur, schema, table, rows.get(table) or [])
                summary[table] = len(rows.get(table) or [])
                done.append(table)
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        finally:
            conn.close()
        raise SyncError(f"{step[0]} {step[1]}: {type(exc).__name__}: {exc}") from exc
    try:  # statistiques : seul le proprietaire peut ANALYZE ; sinon PostgreSQL passe (avertissement), autovacuum suit
        with conn.cursor() as cur:
            for table in done:
                cur.execute(sql.SQL("analyze {}").format(_table(schema, table)))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    if warnings:
        summary["warnings"] = warnings
    return summary
```

Note for the fake: `conn.close()` may be called twice in no path; the analyze block runs on the same open connection.

- [ ] **Step 4:** tests pass. **Step 5:** commit `feat(db-sync): one-transaction load into a self-hosted PostgreSQL`.

### Task 4: dry-run check and `db-sync` command

**Files:** Modify `src/tsi_agent/db_sync.py`, `src/tsi_agent/__main__.py`; Test `tests/test_db_sync.py`, `tests/test_cli.py`

- [ ] **Step 1: failing tests**

```python
from tsi_agent.db_sync import check


class CheckCursor(FakeCursor):
    def execute(self, stmt, params=None):
        text = stmt if isinstance(stmt, str) else stmt.as_string()
        self.conn.log.append((text, params))
        if text.startswith("select to_regclass"):
            self._one = (None if params[0].split(".")[1] in self.conn.missing else params[0],)
        elif text.startswith("select has_table_privilege"):
            self._one = (params[1] not in self.conn.readonly,)


class CheckConn(FakeConn):
    def __init__(self, missing=(), readonly=()):
        super().__init__(missing)
        self.readonly = set(readonly)

    def cursor(self):
        return CheckCursor(self)


def test_check_reports_missing_and_not_writable_and_writes_nothing():
    conn = CheckConn(missing={"csrs", "csr_recitals"}, readonly={"projects"})
    report = check("u", "public", "service_role", connect=lambda url: conn)
    assert report["missing"] == ["csrs"]
    assert report["missing_optional"] == ["csr_recitals"]
    assert report["not_writable"] == ["projects"]
    assert conn.rollbacks == 1 and conn.commits == 0
    assert not any(s.startswith(("delete", "insert")) for s in statements(conn))
```

CLI test in `tests/test_cli.py` (follow the file's existing pattern of calling `main([...])` with `--settings`):

```python
def test_db_sync_without_url_is_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TSI_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["db-sync", "--dry-run"]) == 1   # adapt the call to the helper the file uses for settings/sources
    assert "TSI_DATABASE_URL" in capsys.readouterr().err
```

- [ ] **Step 2:** run → fail.
- [ ] **Step 3: implementation**

```python
MANDATORY = (*DELETE_ORDER, IMPACT_TABLE)
_WRITABLE = """select has_table_privilege(current_user, c.oid, 'INSERT') and has_table_privilege(current_user, c.oid, 'DELETE')
  and (not c.relrowsecurity or pg_get_userbyid(c.relowner) = current_user
       or (select r.rolbypassrls from pg_roles r where r.rolname = current_user)
       or exists (select 1 from pg_policies p where p.schemaname = n.nspname and p.tablename = c.relname
                  and p.cmd in ('ALL', 'DELETE') and (current_user = any(p.roles) or 'public' = any(p.roles))))
from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = %s and c.relname = %s"""


def check(url: str, schema: str, role: str = "", *, connect: Callable[[str], Any] = _connect) -> dict[str, Any]:
    """Verification sans ecriture : tables presentes et modifiables par le role effectif (droits et securite par ligne)."""
    from psycopg import sql
    try:
        conn = connect(url)
    except Exception as exc:
        raise SyncError(f"connexion a la base impossible : {type(exc).__name__}: {exc}") from exc
    report: dict[str, Any] = {"schema": schema, "role": role or "(compte de connexion)", "missing": [],
                              "missing_optional": [], "not_writable": []}
    try:
        with conn.cursor() as cur:
            if role:
                cur.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            for table in (*MANDATORY, *(o[0] for o in OPTIONAL_TABLES)):
                if not _exists(cur, schema, table):
                    report["missing" if table in MANDATORY else "missing_optional"].append(table)
                    continue
                cur.execute("select has_table_privilege" + _WRITABLE[len("select has_table_privilege"):],
                            (schema, table))
                if not cur.fetchone()[0]:
                    report["not_writable"].append(table)
    finally:
        conn.rollback()
        conn.close()
    return report
```

(`_WRITABLE` starts with `select has_table_privilege`; execute it directly: `cur.execute(_WRITABLE, (schema, table))`.)

`__main__.py`: parser, after `supabase-sync`:

```python
    dbs = sub.add_parser("db-sync", help="charger projets, CSR, liens et anomalies dans la base PostgreSQL d'une "
                                         "installation auto-hebergee (connexion directe, une transaction)")
    dbs.add_argument("--dry-run", action="store_true", help="verifier connexion, tables et droits sans rien ecrire")
```

Command, after the `supabase-sync` branch:

```python
    if args.command == "db-sync":
        from tsi_agent.db_sync import check, database_settings, load
        from tsi_agent.supabase_sync import SyncError, build_rows
        try:
            url, schema, role = database_settings()
            rows = build_rows(Path(settings.output_dir))
            if args.dry_run:
                report = {**check(url, schema, role), "rows": {t: len(v) for t, v in rows.items()}}
                print(json.dumps(report, indent=2, ensure_ascii=False))
                return 1 if report["missing"] or report["not_writable"] else 0
            summary = load(rows, url, schema, role, backup=Path(settings.output_dir) / "state" / "db_rows.json")
        except (SyncError, OSError) as exc:
            print(f"ERREUR : {exc} — base inchangee (transaction annulee)", file=sys.stderr)
            return 1
        for warning in summary.get("warnings") or []:
            print(f"ATTENTION : {warning}", file=sys.stderr)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
```

- [ ] **Step 4:** tests pass. **Step 5:** commit `feat(db-sync): --dry-run checks tables and write access; command`.

### Task 5: bench validation of db-sync

- [ ] Start the bench (command in the header), `PGCLIENTENCODING=UTF8`.
- [ ] Record counts of every table in both databases (`select count(*)` per table) to `scratchpad/before-*.txt`.
- [ ] `tsi_schema_test`, client case: `alter role service_role nobypassrls;` then with
  `TSI_DATABASE_URL=postgresql://tsi_authenticator:test@localhost:55432/tsi_schema_test`, `TSI_DB_SCHEMA=tsi_explorer`,
  `TSI_DB_ROLE=service_role`: `python -m tsi_agent db-sync --dry-run` (expect exit 0, no missing, nothing not writable),
  then `db-sync` (expect the counts of `build_rows`), then again (same counts). Check `testers`, `user_passwords`,
  `link_reviews`, `page_views`, `impact_reports where source='web'` unchanged. Restore `alter role service_role bypassrls;`.
- [ ] `tsi_explorer` (public) as `tsi_owner`, no role: same.
- [ ] Negative: wrong schema → dry-run exit 1 listing missing tables; a failing load (e.g. `TSI_DB_ROLE=anon`) → exit 1, counts unchanged.
- [ ] Page http://localhost:8080/ (if the app service runs) shows the data; otherwise query `tsi_dashboard()`.
- [ ] Integration test file `tests/test_db_sync_integration.py` marked `integration`, using `TSI_TEST_DATABASE_URL`
  (skip if unset): load `build_rows` of a small fixture output into the database, compare counts, run twice.
- [ ] Stop the bench. Commit tests.

### Task 6: ECFIN year follows the export

**Files:** `src/tsi_agent/csr.py`, `src/tsi_agent/__main__.py` (`--year-to` default None); Test `tests/test_csr.py`

- [ ] **Step 1: failing test** (reuse the xlsx row helper of `tests/test_csr.py`; rows of 2025 and 2026, and 2099):

```python
def test_build_csrs_default_year_to_is_latest_year_of_the_export():
    rows = [_row("Croatia", "2025", "1"), _row("Croatia", "2026", "1"), _row("Croatia", "2099", "1")]
    years = {c.year for c in build_csrs(rows)}
    assert years == {2025, 2026}
    assert {c.year for c in build_csrs(rows, year_to=2025)} == {2025}
```

- [ ] **Step 2:** fail (2026 missing).
- [ ] **Step 3:** in `csr.py`: `YEAR_FROM = 2016` stays; replace `YEAR_TO = 2025` by a comment that the last year is
  the latest year of the export (capped at the current year); `build_csrs(rows, year_from=YEAR_FROM, year_to: int | None = None)`:
  `if year_to is None: year_to = date.today().year` (rows of later years are not real). In `__main__.py`:
  `csrb.add_argument("--year-to", type=int, default=None, help="derniere annee (defaut : la plus recente de l'export)")`.
- [ ] **Step 4:** `pytest tests/test_csr.py tests/test_csr_link.py tests/test_supabase_sync.py -q` pass.
- [ ] **Step 5:** commit `feat(csr): adopted CSRs up to the latest year of the ECFIN export`.

### Task 7: Commission recommendations per year

**Files:** `src/tsi_agent/csr_commission.py`, `src/tsi_agent/__main__.py`, `src/tsi_agent/supabase_sync.py`, `src/tsi_agent/csr_related.py` (default year); Test `tests/test_csr_commission.py`

- [ ] **Step 1: failing tests**

```python
from tsi_agent import csr_commission as cc


def test_publication_ids_per_year():
    assert cc.publication_id(2027) == "2027-european-semester-country-specific-recommendations-commission-recommendations"
    assert cc.publication_url(2027).endswith("/publications-0/" + cc.publication_id(2027) + "_en")
    assert cc.PUBLICATION_ID == cc.publication_id(2026)


def test_source_years_latest_year_and_all_year_loaders(tmp_path):
    out = tmp_path / "output"
    for year in (2026, 2027):
        cc.save_source(out, TEXT.replace("2026", str(year)), year)
    assert cc.source_years(out, today=2027) == [2026, 2027]
    assert cc.latest_year(out, today=2027) == 2027
    assert cc.latest_year(tmp_path / "empty", today=2027) == cc.FIRST_YEAR
    years = {c.year for c in cc.commission_csrs(out, today=2027)}
    assert years == {2026, 2027}
    assert {r.year for r in cc.all_recitals(out, today=2027)} <= {2026, 2027}


def test_adopted_year_replaces_commission_recommendations_but_keeps_recitals(tmp_path):
    out = tmp_path / "output"
    cc.save_source(out, TEXT, 2026)
    adopted = [c for c in cc.commission_csrs(out, today=2026)]
    adopted = [type(c)(c.country, 2026, c.number, c.full, []) for c in adopted]   # same years as ECFIN rows
    merged = cc.with_commission(adopted, cc.commission_csrs(out, today=2026))
    assert all(c.stage != cc.COMMISSION_STAGE for c in merged)
    assert cc.all_recitals(out, today=2026)
```

(`TEXT` is the existing fixture of the file; check that its documents mention 2026 in a way `replace` keeps parseable.)

- [ ] **Step 2:** fail.
- [ ] **Step 3: implementation** in `csr_commission.py`:

```python
FIRST_YEAR = 2026   # premiere annee publiee par la Commission dans ce format (avant : base ECFIN seulement)
YEAR = FIRST_YEAR   # compatibilite : annee par defaut des fonctions ci-dessous


def publication_id(year: int) -> str:
    return f"{year}-european-semester-country-specific-recommendations-commission-recommendations"


def publication_url(year: int) -> str:
    return f"https://reforms-investments.ec.europa.eu/publications-0/{publication_id(year)}_en"


PUBLICATION_ID = publication_id(YEAR)
PUBLICATION_URL = publication_url(YEAR)
```

`source_paths(output_dir, year=YEAR)`, `find_source(output_dir, year=YEAR)` use `publication_id(year)`;
`load(output_dir, year=None)`: `year = year or latest_year(output_dir)`; `save_source(output_dir, text, year=YEAR)`;
`merge_texts(existing, added, year=YEAR)` (header `# {publication_id(year)}`); `fetch_missing(..., page_url=PUBLICATION_URL, ...)` unchanged.

```python
def source_years(output_dir: Path, today: int | None = None) -> list[int]:
    """Annees dont le texte de la publication est present localement, de FIRST_YEAR a l'annee en cours."""
    last = today or date.today().year
    return [y for y in range(FIRST_YEAR, last + 1) if find_source(output_dir, y)]


def latest_year(output_dir: Path, today: int | None = None) -> int:
    years = source_years(output_dir, today)
    return years[-1] if years else FIRST_YEAR


def commission_csrs(output_dir: Path, today: int | None = None) -> list[Csr]:
    return [c for y in source_years(output_dir, today) for c in load(output_dir, y).csrs]


def all_recitals(output_dir: Path, today: int | None = None) -> list[Recital]:
    return [r for y in source_years(output_dir, today) for r in load(output_dir, y).recitals]
```

`load_all_csrs`: `with_commission(csrs, commission_csrs(output_dir))`.
`supabase_sync.build_rows`: recitals from `all_recitals(output_dir)` (import it instead of `load as load_commission`).
`__main__.py` `csr-link`: `csr_commission.commission_csrs(Path(settings.output_dir))`; `csr-related`: default year
`csr_commission.latest_year(Path(settings.output_dir))`; `csr-build`: `_commission` returns `(commission, year)`:

```python
def _commission(settings: Settings, fetch_missing: bool):
    """(recommandations de la Commission, annee) : annee la plus recente au texte local ; avec `fetch_missing`, la
    publication de l'annee en cours est essayee (absente : annee precedente gardee, sans erreur), puis les PDF des pays
    manquants sont telecharges. None (message affiche) si le telechargement des pays manquants echoue."""
    from tsi_agent import csr_commission
    from tsi_agent.countries import COUNTRIES
    from tsi_agent.http import FetchError, shared_fetcher
    output_dir = Path(settings.output_dir)
    year = csr_commission.latest_year(output_dir)
    current = date.today().year
    fetcher = None
    if fetch_missing and current > year:
        fetcher = shared_fetcher(settings.cache_dir, settings.min_interval_s, settings.user_agent)
        try:
            added = csr_commission.fetch_missing(fetcher, list(COUNTRIES), csr_commission.publication_url(current))
        except (FetchError, OSError) as exc:
            print(f"(CSR {current} : publication de la Commission pas encore disponible : {exc})", file=sys.stderr)
            added = ""
        if added:
            csr_commission.save_source(output_dir, csr_commission.merge_texts(None, added, current), current)
            year = current
    commission = csr_commission.load(output_dir, year)
    if not fetch_missing or not commission.missing_countries:
        return commission, year
    try:
        fetcher = fetcher or shared_fetcher(settings.cache_dir, settings.min_interval_s, settings.user_agent)
        added = csr_commission.fetch_missing(fetcher, commission.missing_countries, csr_commission.publication_url(year))
    except (FetchError, OSError) as exc:
        print(f"ERREUR : téléchargement des PDF CSR impossible : {exc}", file=sys.stderr)
        return None
    if added:
        path = csr_commission.find_source(output_dir, year)
        existing = path.read_text(encoding="utf-8") if path else None
        csr_commission.save_source(output_dir, csr_commission.merge_texts(existing, added, year), year)
        commission = csr_commission.load(output_dir, year)
    return commission, year
```

In `csr-build`: `got = _commission(...)`; `if got is None: return 1`; `commission, cyear = got`;
`csrs = with_commission(csrs, commission_csrs(output_dir))` (all years, adopted years win); messages use `cyear`.
Missing countries of a year already adopted in ECFIN are not alerted (`if cyear not in {c.year for c in ecfin}`).
`date` import in `__main__.py` if missing.

- [ ] **Step 4:** `pytest tests/test_csr_commission.py tests/test_supabase_sync.py tests/test_csr_link.py tests/test_csr_related.py tests/test_cli.py -q` pass.
- [ ] **Step 5:** commit `feat(csr): Commission recommendations and recitals of every published Semester year`.

### Task 8: country reports year fallback

**Files:** `src/tsi_agent/country_reports.py`, `src/tsi_agent/__main__.py`; Test `tests/test_country_reports.py`

- [ ] **Step 1: failing tests** (fake fetcher like the existing `test_fetch_reports_writes_markdown_and_skips_existing`)

```python
def test_fetch_reports_tries_current_year_then_keeps_previous(tmp_path):
    # page links only a 2026 report; today = 2027: the 2026 file exists -> skipped, nothing failed
    ...
    summary = fetch_reports(out, fetcher, ["HR"], today=2027)
    assert summary["skipped"] == ["HR"] and not summary["failed"]


def test_fetch_reports_downloads_new_year_when_published(tmp_path):
    # page links a 2027 report; today = 2027 -> HR-2027.md written
    ...


def test_load_reports_latest_year_per_country(tmp_path):
    # HR-2026.md and HR-2027.md, PT-2026.md -> HR 2027 and PT 2026
```

(Write the three bodies with the existing fixture helpers of the file: page HTML with `_HREF` links
`/document/download/x_en?filename=..SWD_<year>_...pdf`, fake fetcher returning that page and `%PDF` bytes, reader
returning `(1, "text")`.)

- [ ] **Step 2:** fail.
- [ ] **Step 3: implementation**

```python
def fetch_reports(output_dir, fetcher, countries=None, year: int | None = None, reader=None, force=False,
                  today: int | None = None):
    """Rapports absents ; sans `year` : annee en cours, sinon precedente (jamais avant YEAR). Resume {fetched, skipped, failed}."""
    ...
    last = today or date.today().year
    years = [year] if year else [y for y in (last, last - 1) if y >= YEAR]
    for country in countries or list(COUNTRIES):
        page_html = None
        outcome = None
        for y in years:
            if not force and report_path(output_dir, country, y).exists():
                outcome = "skipped"
                break
            try:
                if page_html is None:
                    page_html = fetcher.get(page_url(country)).content.decode("utf-8", "replace")
                pdf = find_pdf(page_html, y)
                if not pdf:
                    continue
                data = fetcher.get(pdf, cache=False).content
                ...  # unchanged checks, write_report(..., Report(country, y, pdf, url, pages, text))
                outcome = "fetched"
                break
            except FetchError / Exception: failed[country] = ...; outcome = "failed"; break
        if outcome is None:
            summary["failed"][country] = f"no {'/'.join(map(str, years))} English report link on {page_url(country)}"
        elif outcome in ("skipped", "fetched"):
            summary[outcome].append(country)
```

`load_reports`: after reading, keep the highest year per country: `latest = {}` by country, return sorted by country.

- [ ] **Step 4:** `pytest tests/test_country_reports.py tests/test_supabase_sync.py -q` pass.
- [ ] **Step 5:** commit `feat(country-reports): current Semester year when published, latest year per country`.

### Task 9: web page follows the data years

**Files:** `web/app.js`; Test `tests/js/app-load.test.mjs` (loads), manual check.

- [ ] **Step 1:** in `web/app.js`:
  - `REPORTS_YEAR` and `RECITALS_YEAR` become `let` (2026 until loaded), declared together near the top (before
    any use, to avoid the temporal dead zone the load test guards), plus `async function loadDataYears()` that reads
    `csr_recitals` and `country_reports` `select("year").order("year", { ascending: false }).limit(1)` and sets
    them (errors ignored); called once after sign-in where the dashboard data starts loading.
  - `REPORTS_ABOUT` and `RECITALS_ABOUT` become functions `reportsAbout()` / `recitalsAbout()`; "Available for
    ${RECITALS_YEAR} only." becomes "Available from 2026 (the Commission's documents)".
  - `countryReport(country, year)`: drop `if (year !== RECITALS_YEAR) return null` (the query returns null when there is none).
  - CSR document page: `docRecitals(d.doc_id)` for every year ≥ 2026 (`year >= 2026` instead of `year === RECITALS_YEAR`).
  - `recitalMatchCount`: `if (!f.q) return null;` and `if (f.year) q = q.eq("year", f.year);`.
- [ ] **Step 2:** `node --test tests/js` pass.
- [ ] **Step 3:** commit `feat(web): recitals and country reports of any Semester year; labels from the data`.

### Task 10: `deploy/onprem/update.sh`

**Files:** Create `deploy/onprem/update.sh` (LF, executable bit via `git update-index --chmod=+x`); Test `tests/test_onprem_update_sh.py`

- [ ] **Step 1: failing tests** (skip without bash): a temporary root with `deploy/onprem/update.sh` copied,
  `.github/scripts/ci_summary.py` replaced by a stub, `config/settings.yaml` (`llm_provider: openai`), `.env`
  (`OPENAI_API_KEY=k`, `TSI_DATABASE_URL=u`), and `TSI_PYTHON` pointing to a fake python script that appends its
  arguments to `calls.txt` and exits with the code given in `FAKE_CODES` (`step=code` pairs) for its step.

```python
def test_steps_in_order_and_exit_0(tmp_path): ...       # calls: run, deliverables, csr-fetch, csr-build --fetch-missing,
                                                         # country-reports, csr-link, anomalies, db-sync
def test_quota_is_a_warning_exit_1(tmp_path): ...       # FAKE_CODES=run=1 -> all steps run, exit 1
def test_sitemap_is_exit_3(tmp_path): ...               # run=3 -> exit 3
def test_strict_failure_stops(tmp_path): ...            # csr-build=2 -> no anomalies/db-sync, exit 2
def test_db_sync_retried_once(tmp_path): ...            # db-sync=1 -> called twice, exit 1 (UPDATE_RETRY_WAIT_S=0)
def test_options_skip_and_recheck(tmp_path): ...        # --skip-deliverables --skip-csr --recheck-all --no-sync
def test_scheduled_full_check_in_quarter_month(tmp_path): ...  # UPDATE_MONTH=4 --scheduled -> run --recheck-all
def test_lock_refuses_second_run(tmp_path): ...         # lock dir present -> exit 75
```

- [ ] **Step 2:** fail (script missing).
- [ ] **Step 3: implementation** — bash, `set -u`; root `$(cd "$(dirname "$0")/../.." && pwd)`; `PY=${TSI_PYTHON:-$ROOT/.venv/bin/python}`;
  options parsed in a `while` loop; settings value read with `sed -n "s/^${key}:[[:space:]]*\([^#]*\).*/\1/p"`;
  key presence checked in the environment then `.env` (lines `KEY=value`, value not empty); lock: `flock -n 9` on
  `logs/update.lock` when `flock` exists, else `mkdir logs/update.lock.d` (removed by `trap ... EXIT`); exit 75
  when held; log `logs/update-$(date +%Y%m%d-%H%M).log` through `exec > >(tee -a "$LOG") 2>&1`; `step name policy args...`
  function runs `"$PY" -m tsi_agent --settings "$SETTINGS" "$@"`, records `name\tcode\tseconds\tpolicy` in
  `$WORK/steps.tsv`; policies as in the spec table (`quota`: 1 and 3 warnings, else stop; `soft`: warning;
  `strict`: stop); `db-sync` retried after `${UPDATE_RETRY_WAIT_S:-60}` s; month from `${UPDATE_MONTH:-$(date +%-m)}`;
  final `"$PY" .github/scripts/ci_summary.py --runs-dir logs/runs --work-dir "$WORK" --since "$SINCE" --mode "$MODE"`
  (ignored failure); exit worst code (1 before 3), or the stop code.
- [ ] **Step 4:** `pytest tests/test_onprem_update_sh.py -q` pass (Git Bash on Windows).
- [ ] **Step 5:** commit `feat(onprem): update.sh, the Linux monthly update`.

### Task 11: updater container

**Files:** Create `deploy/onprem/docker/updater.Dockerfile`, `deploy/onprem/docker/compose.updater.yaml`,
`deploy/onprem/docker/updater.env.example`; `.dockerignore` at the root if missing (exclude `.venv`, `output`,
`.cache`, `logs`, `.env`, `server/node_modules`, `*.zip`, `User Feedback`).

```dockerfile
# Monthly data update of the self-hosted TSI x CSR Explorer (deploy/onprem/README.md, step 11).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY deploy/onprem/update.sh deploy/onprem/update.sh
COPY deploy/updater/settings.updater.yaml deploy/updater/settings.updater.yaml
COPY .github/scripts/ci_summary.py .github/scripts/ci_summary.py
RUN pip install --no-cache-dir . && useradd --uid 1000 --create-home tsi \
    && mkdir -p output .cache logs && chown -R tsi /app
USER tsi
ENV TSI_PYTHON=python PYTHONUNBUFFERED=1
VOLUME ["/app/output", "/app/.cache", "/app/logs"]
ENTRYPOINT ["bash", "deploy/onprem/update.sh"]
```

```yaml
# Updater of the self-hosted TSI x CSR Explorer: started by the host's cron, never by "up".
#   podman-compose -f compose.updater.yaml run --rm updater --scheduled
services:
  updater:
    build:
      context: ../../..
      dockerfile: deploy/onprem/docker/updater.Dockerfile
    image: tsi-explorer-updater
    env_file: updater.env
    volumes:
      - tsi-update-output:/app/output
      - tsi-update-cache:/app/.cache
      - tsi-update-logs:/app/logs
      - ./settings.yaml:/app/config/settings.yaml:ro
volumes:
  tsi-update-output:
  tsi-update-cache:
  tsi-update-logs:
```

`updater.env.example`: `OPENAI_API_KEY=`, `TSI_DATABASE_URL=postgresql://<user>:<password>@<db-host>:5432/tsi_explorer?sslmode=require`,
`TSI_DB_SCHEMA=tsi_explorer`, `TSI_DB_ROLE=service_role`, `HTTPS_PROXY=`, `HTTP_PROXY=`, `NO_PROXY=`, with comments
(same account and password as PostgREST; a password with special characters must be percent-encoded in the URL).

- [ ] Check: `bash -n deploy/onprem/update.sh`; YAML parses (`python -c "import yaml,sys; yaml.safe_load(open(...))"`).
  The image cannot be built on this PC (no Docker): stated in the handover.
- [ ] Commit `feat(onprem): updater container (Docker or Podman)`.

### Task 12: documentation

**Files:** `deploy/onprem/README.md`, `README.md`

- [ ] Status table: "Data load into a self-hosted database (`db-sync`, direct connection, one transaction)" and
  "Linux update script and updater container" → **Ready** (tested on PostgreSQL 16; container not built at Agilos).
- [ ] Architecture line: `monthly data update (container or cron) ──TCP 5432──▶ database`.
- [ ] Step 10: settings (`TSI_DATABASE_URL`, `TSI_DB_SCHEMA`, `TSI_DB_ROLE`, grant `service_role` to the login
  account if `TSI_DB_ROLE=service_role`: one SQL line for the DBA, runnable in a query tool), `db-sync --dry-run`,
  the steps with `csr-build --fetch-missing` and `db-sync`; replace the PostgREST-based planned settings.
- [ ] Step 11: `update.sh` (options, exit codes, lock, log) and the container variant (build, first state from the
  release `tsi-output-state.zip` into the output volume, cron line with podman-compose, proxy variables).
- [ ] Note on Semester years (what is picked up automatically each year, the assistant texts still saying 2026).
- [ ] Root README getting-started row: self-hosted includes the monthly update.
- [ ] Commit `docs(onprem): monthly update (db-sync, update.sh, updater container)`.

### Task 13: full verification

- [ ] `pytest -q` (all), `node --test tests/js`, `bash -n deploy/onprem/update.sh`.
- [ ] Code review of the branch (superpowers:requesting-code-review).
- [ ] Tell haystack-pipeline-04 and haystack-pipeline-cb the branch is done; put the shared folder back on
  `feature/country-reports` only if the user asks to merge.
