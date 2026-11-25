# On-premises monthly update: direct PostgreSQL load, Semester years, Docker updater — design

Date: 2026-11-25 · Branch: `feature/onprem-update` (from `feature/country-reports` at d6191ac, after the merge of
`feature/onprem-assistant`)

## Goal

The self-hosted installation already runs in Docker on Linux: the web page, the application service (with the
assistant on GPT@EC) and PostgREST, on a **local PostgreSQL**. The monthly reload must bring in the new projects,
publications, recommendations (CSRs), recitals, country reports and project → CSR links, and load them into that
database. Today (deploy/onprem/README.md, steps 10 and 11) the load and the Linux update script are "planned".

Findings that shape the work:

- Every LLM call of the update (`run` and its sitemap judge, `deliverables`, `csr-link` with the policy areas)
  goes through the configured provider: `llm_provider: openai` with GPT@EC already works. No web search is used by
  the update (only the impact analysis searches the web; it is not part of the reload).
- `supabase-sync` only writes to Supabase. There is no load into a self-hosted database.
- The years are fixed in the code: ECFIN CSRs up to 2025 (`csr.py`, `csr-build --year-to`), Commission
  recommendations and recitals 2026 (`csr_commission.py`), country reports 2026 (`country_reports.py`), and the
  web page (`RECITALS_YEAR`, `REPORTS_YEAR`, `RELATED_YEAR`). The 2026 CSRs adopted by the Council and the 2027
  Commission recommendations would be missed.

Out of scope: the impact analysis web search; the Windows updater (unchanged, apart from shared Python code);
the application service and its sign-in; the Docker files of the client's stack (not in the repository).

## Decisions

| Question | Decision |
|---|---|
| Write path to the database | **Direct PostgreSQL connection** (psycopg 3), **one transaction**: on any error the database keeps its previous content. |
| Command | New `python -m tsi_agent db-sync [--dry-run]`. `supabase-sync` is unchanged (cloud). |
| Rows | Same rows as the cloud: `build_rows()` of `tsi_agent.supabase_sync`, unchanged. |
| Account | The PostgREST login account (`tsi_authenticator`), which switches to `service_role` (`set local role`, setting `TSI_DB_ROLE`), as deploy/onprem/sql/900_service_role.sql already describes for the data updates. Works without BYPASSRLS (the `service_role_all` rules). The owner account also works (`TSI_DB_ROLE` empty). No new role, no new grant. |
| Connection settings | `TSI_DATABASE_URL` (libpq URL, e.g. `...?sslmode=require`), optional `TSI_DB_SCHEMA` (default `public`; `tsi_explorer` on the client's server) and `TSI_DB_ROLE` (default empty), environment or `.env`; never in Git. |
| Years | Follow the data: latest year present in the ECFIN export; latest Commission publication available; latest country report available. |
| Update script | `deploy/onprem/update.sh`, the Linux version of `deploy/updater/update.ps1`. |
| Docker | An updater image (`deploy/onprem/docker/updater.Dockerfile`) and a compose snippet to add to the client's stack; the host's cron runs it monthly. |
| Dependency | `psycopg[binary]>=3.1` in the main dependencies. |

## Part A — `db-sync` (`src/tsi_agent/db_sync.py`)

### Settings

`database_settings(dotenv=Path(".env")) -> (url, schema)`: `TSI_DATABASE_URL` from the environment, else `.env`
(`read_dotenv`, which ignores a `.env` that is a directory); required. `TSI_DB_SCHEMA` the same way, default
`public`, must match `[a-z_][a-z0-9_]{0,62}`. Otherwise `SyncError` with a clear message (the URL is never printed:
it holds the password).

### Load

`load(rows, url, schema, *, connect=psycopg.connect, backup=None) -> dict`:

1. `backup`: JSON of the rows written before anything (`output/state/db_rows.json`).
2. Guard: the tables written never include `testers`, `link_reviews`, `page_views`, `user_passwords`.
3. One connection, one transaction:
   - `set local role <TSI_DB_ROLE>` when set; `set local search_path` is not used (names are schema-qualified);
   - `delete from <schema>.<table>` in `DELETE_ORDER` (links, parts, csrs, csr_documents, projects, anomalies);
   - `insert into <schema>.<table> (<cols>) select <cols> from json_populate_recordset(null::<schema>.<table>, %s)`
     in `INSERT_ORDER`, 500 rows per statement; columns = keys of the rows (same keys for every row of a table).
     Generated (`fts`) and identity (`id`) columns are never named;
   - `impact_reports`: `delete ... where source = 'local'`, then the local reports (analyses run from the page,
     `source = 'web'`, are kept);
   - optional tables (`csr_related_projects`, `csr_recitals`, `country_reports`, `country_report_passages`):
     `to_regclass` null → skipped, same warning as `supabase-sync`; otherwise delete all and insert;
   - commit, then `analyze` of every table written (outside the transaction; only the owner may analyze: under
     another role PostgreSQL skips it with a notice, and autovacuum does it later).
4. Any exception: rollback, `SyncError` naming the table and the action.
5. Returns the summary of `supabase-sync` (`{table: rows, ..., "warnings": [...]}`).

Identifiers are composed with `psycopg.sql.Identifier`; values travel as one JSON parameter per batch.

### Command

- `db-sync`: settings, rows, load, JSON summary, exit 0.
- `--dry-run`: rows built, connection opened, mandatory tables checked (`to_regclass`), counts and missing optional
  tables printed, nothing written, exit 0 (exit 1 if a mandatory table is missing).
- `SyncError`, `psycopg.Error`, `OSError`: `ERREUR : ... — base inchangee (transaction annulee)`, exit 1.

## Part B — Semester years

### ECFIN CSRs (`csr.py`, `csr-build`)

`csr-build --year-to` defaults to the **latest year present in the export** (capped at the current year) instead
of 2025. `build_csrs(rows, year_from, year_to=None)`: `None` = that latest year.

### Commission recommendations and recitals (`csr_commission.py`)

- `publication_id(year)` / `publication_url(year)` replace the fixed `PUBLICATION_ID` / `PUBLICATION_URL`
  (same pattern `<year>-european-semester-country-specific-recommendations-commission-recommendations`).
- Year of the Commission stage = the **latest year with a local source text**, looked for from the current year
  down to 2026; `csr-build` in the update tries the current year's publication when its text is missing
  (`fetch_missing` with all 27 countries; page absent (404) → previous year kept, no error).
- When the ECFIN export already holds the adopted CSRs of that year, the Commission recommendations of that year
  are **not** added (the adopted ones win); the recitals of that year are still loaded (they only exist in the
  Commission text).
- Module constant `YEAR` kept as the first year of the Commission stage (2026) for compatibility; callers use
  the detected year.

### Country reports (`country_reports.py`, `country-reports`)

`fetch_reports` tries the current year, then the previous one, per country; reports already downloaded are kept.
`load_reports` returns the **latest year per country** only (the web page and the assistant describe one year).

### Web page (`web/app.js`)

`RECITALS_YEAR`, `REPORTS_YEAR`, `RELATED_YEAR` come from the data (latest year in `csr_recitals`,
`country_reports`, `csr_related_projects`), with 2026 as the value until loaded. Texts that say "2026" in the
guide keep it where they describe the current data set.

### Assistant (coordination, not in this branch)

The assistant's prompts (`supabase/functions/tsi-assistant/index.ts`, used in process by the self-hosted
service) say "2016-2026", "2026 Commission recommendations", "2026 country reports". They stay true until the 2027
data arrives (around June 2027). The session that owns the assistant is told; making them follow the data is a
separate change.

## Part C — `deploy/onprem/update.sh`

Bash; `flock` and `timeout` (util-linux, coreutils). Runs at the root of the repository (`$(dirname "$0")/../..`),
with `.venv/bin/python`, or `python` when `TSI_PYTHON` is set (the Docker image).

```
update.sh [--recheck-all] [--scheduled] [--skip-deliverables] [--skip-csr] [--detail-only] [--no-sync]
          [--prod-export] [--settings <file>]
```

| Step | Policy |
|---|---|
| `run [--recheck-all]` | quota: exit 1 and 3 warnings; 2 or other stops |
| `deliverables [--recheck-all]` (unless `--skip-deliverables`) | quota |
| `csr-fetch` (unless `--skip-csr`) | soft |
| `csr-build --fetch-missing` | strict |
| `country-reports` | soft |
| `csr-link [--detail-only]` | quota |
| `anomalies` | strict |
| `db-sync` (unless `--no-sync`) | soft, second attempt after 60 s |
| `prod-export` (with `--prod-export`) | strict |

- `--scheduled`: full check (`--recheck-all`) in January, April, July and October.
- Checks first: settings file; in `openai` mode the key named by `openai_api_key_env`; with sync,
  `TSI_DATABASE_URL` (missing → sync skipped, warning).
- Lock: `flock -n logs/update.lock`; already running → exit 75.
- Log: `logs/update-YYYYMMDD-HHMM.log`; `steps.tsv` and `.github/scripts/ci_summary.py` summary at the end.
- Exit code: 0; 1 warning; 3 sitemap unreadable; 2 (or the step's code) stopped. Worst wins (1 before 3).

## Part D — Docker updater

Client's server (from the session that set up the sign-in): **Podman with podman-compose** in `~/tsi-docker`,
services `postgrest`, `app`, `nginx`, secrets as variables in `~/tsi-docker/.env`; **external PostgreSQL**
(`sslmode=require`, schema `tsi_explorer`, `service_role` without BYPASSRLS); an HTTP proxy on the host (internal
hosts in `NO_PROXY`); the DBA runs SQL in a query tool (no psql); podman-compose: `down` then `up -d`, never
`--force-recreate`. The files below work with both Docker and Podman (no compose profile: older podman-compose
versions ignore profiles, so the updater is a separate compose file run with `run --rm`).

- `deploy/onprem/docker/updater.Dockerfile`: `python:3.12-slim`, `pip install .` (no `.[local]`), code copied,
  `TSI_PYTHON=python`, entrypoint `deploy/onprem/update.sh`. No secret in the image.
- `deploy/onprem/docker/compose.updater.yaml`: service `updater` alone (its own file, started only by `run`),
  volume `tsi-update-data` for `/app/output`, `/app/.cache`, `/app/logs`; `env_file` with `OPENAI_API_KEY`,
  `TSI_DATABASE_URL`, `TSI_DB_SCHEMA`, `TSI_DB_ROLE`, `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`; settings file
  mounted read-only (`deploy/updater/settings.updater.yaml` as `config/settings.yaml`). The database is external:
  no network shared with the stack is needed.
- Host cron of the user that runs Podman: `podman-compose -f compose.updater.yaml run --rm updater --scheduled`
  (or `docker compose ...`) on the first Sunday of the month.
- First start: the state (`output/`) from the release `tsi-output-state.zip` unpacked into the volume
  (documented command), otherwise the first run reads everything (about 3 hours).

## Part E — documentation

`deploy/onprem/README.md`: status table (load and update script **Ready**), architecture line, steps 10 and 11
(`.env`, `db-sync --dry-run`, `update.sh`, Docker variant, cron), operations (logs). Root `README.md`: the
self-hosted row mentions the update.

## Error handling

- Database unreachable, wrong password, missing table: `db-sync` exits 1, nothing written.
- Constraint violation: rollback, exit 1 with table and error.
- Commission publication of the year not out yet: previous year kept, no warning; text of some countries
  missing: the existing warning of `csr-build`.
- `update.sh`: `db-sync` failed twice → warning; `output/` is up to date, the next run (or a manual `db-sync`)
  loads it.

## Testing

- `tests/test_db_sync.py`: fake connection recording statements: order, batches, columns, impact rows, optional
  table missing, rollback on error, protected tables, schema validation, settings, `--dry-run`. Integration tests
  (`integration` marker) against `TSI_TEST_DATABASE_URL` (local PostgreSQL 16 bench): load, row counts, users'
  tables untouched, second load identical.
- Years: `build_csrs` latest year; Commission year detection and the adopted-wins rule; country report year
  fallback and latest-per-country; web year constants from data (`tests/js`).
- `tests/test_onprem_update_sh.py`: `bash -n`; fake `python` recording calls: step order, options, quota → 1,
  sitemap → 3, strict failure → stop, lock. Skipped without bash.
- Manual: `db-sync` on the bench databases `tsi_explorer` (schema `public`) and `tsi_schema_test` (schema
  `tsi_explorer`), as `tsi_authenticator` with `TSI_DB_ROLE=service_role` and without BYPASSRLS (the client's
  case); the page at http://localhost:8080/ still works. The Docker image cannot be built on this PC
  (no Docker): built and run on the client's server.

## Order

1. `db-sync` + tests + command + dependency; bench validation.
2. Semester years (Python, then web) + tests.
3. `update.sh` + tests.
4. Docker updater files.
5. Documentation.
