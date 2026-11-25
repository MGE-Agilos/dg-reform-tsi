#!/usr/bin/env bash
# Monthly data update of the self-hosted TSI x CSR Explorer (deploy/onprem/README.md, step 11).
# Linux version of deploy/updater/update.ps1. Runs, at the root of the repository:
#   run -> deliverables -> csr-fetch -> csr-build --fetch-missing -> country-reports -> csr-link -> anomalies
#   -> db-sync (load into the database, one transaction) -> [prod-export]
#
#   deploy/onprem/update.sh                    # incremental update
#   deploy/onprem/update.sh --scheduled        # from cron: full check in January, April, July and October
#   deploy/onprem/update.sh --recheck-all      # full check: every detail and publication page read again
#   deploy/onprem/update.sh --skip-deliverables --skip-csr --detail-only --no-sync --prod-export --settings <file>
#
# No secret in this file: OPENAI_API_KEY (GPT@EC), TSI_DATABASE_URL, TSI_DB_SCHEMA and TSI_DB_ROLE come from the
# environment (container) or from .env at the root of the repository.
# Python: TSI_PYTHON if set (container image), else .venv/bin/python.
#
# Exit codes: 0 done; 1 warning (LLM quota, or a soft step failed: csr-fetch, country-reports, db-sync);
# 3 warning (the site's sitemap could not be read: run again later); 2 or the code of a strict step: stopped;
# 75 another update is running. Warnings never stop the update; the worst code wins (1 before 3).
# Log: logs/update-YYYYMMDD-HHMM.log, with the summary of the runs at the end.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 2
PY="${TSI_PYTHON:-$ROOT/.venv/bin/python}"
SETTINGS="config/settings.yaml"
RECHECK=0; SCHEDULED=0; SKIP_DELIVERABLES=0; SKIP_CSR=0; DETAIL_ONLY=0; SYNC=1; PROD_EXPORT=0

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --recheck-all) RECHECK=1 ;;
    --scheduled) SCHEDULED=1 ;;
    --skip-deliverables) SKIP_DELIVERABLES=1 ;;
    --skip-csr) SKIP_CSR=1 ;;
    --detail-only) DETAIL_ONLY=1 ;;
    --no-sync) SYNC=0 ;;
    --prod-export) PROD_EXPORT=1 ;;
    --settings)
      [ $# -ge 2 ] || { echo "--settings needs a file" >&2; exit 2; }
      SETTINGS="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

mkdir -p logs
LOCK="logs/update.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || { echo "another update is running (lock $ROOT/$LOCK): nothing done" >&2; exit 75; }
else
  mkdir "$LOCK.d" 2>/dev/null || { echo "another update is running (lock $ROOT/$LOCK.d): nothing done" >&2; exit 75; }
  trap 'rmdir "$ROOT/$LOCK.d" 2>/dev/null' EXIT
fi

STAMP="$(date +%Y%m%d-%H%M)"
LOG="logs/update-$STAMP.log"
WORK="logs/update-$STAMP"
mkdir -p "$WORK"
exec > >(tee -a "$LOG") 2>&1
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START="$(date +%s)"
WORST=0
WARNINGS=()
MODE="incremental"

warn() {  # warn <message> <1|3>
  echo "WARNING: $1"
  WARNINGS+=("$1")
  if [ "$2" = 1 ]; then WORST=1; elif [ "$2" = 3 ] && [ "$WORST" = 0 ]; then WORST=3; fi
}

finish() {  # finish <exit code>
  echo
  if [ -f .github/scripts/ci_summary.py ]; then
    "$PY" .github/scripts/ci_summary.py --runs-dir logs/runs --work-dir "$WORK" --since "$SINCE" --mode "$MODE" \
      || echo "(summary unavailable)"
  fi
  if [ ${#WARNINGS[@]} -gt 0 ]; then
    echo
    echo "Warnings:"
    for w in "${WARNINGS[@]}"; do echo "  - $w"; done
  fi
  echo
  echo "Finished in $(( $(date +%s) - START )) s with exit code $1. Log: $ROOT/$LOG"
  exit "$1"
}

stop() {  # stop <message> <code>
  echo
  echo "STOPPED: $1"
  finish "$2"
}

step() {  # step <name> <policy> <command args...>: runs python -m tsi_agent, records the result for the summary
  local name="$1" policy="$2" t0 code
  shift 2
  echo
  echo "=== $name : python -m tsi_agent $*  ($(date +%H:%M:%S))"
  t0="$(date +%s)"
  "$PY" -m tsi_agent --settings "$SETTINGS" "$@"
  code=$?
  printf '%s\t%s\t%s\t%s\n' "$name" "$code" "$(( $(date +%s) - t0 ))" "$policy" >> "$WORK/steps.tsv"
  return "$code"
}

llm_step() {  # LLM step (run, deliverables, csr-link): 1 quota and 3 sitemap are warnings, anything else stops
  local name="$1" code
  shift
  step "$name" quota "$@"
  code=$?
  case "$code" in
    0) ;;
    1) warn "$name: LLM quota reached, or key/model refused along the way (exit code 1): see logs/runs/; run again later, it resumes where it stopped" 1 ;;
    3) warn "$name: the site's sitemap could not be read (exit code 3, HTTP 429?): known pages were not re-read; run again later" 3 ;;
    2) stop "$name failed (exit code 2): see the ERREUR (error) message above; if it is 'ERREUR (fournisseur LLM)' (LLM provider error), check the key (OPENAI_API_KEY) and the settings" 2 ;;
    *) stop "$name failed (exit code $code): see logs/runs/" "$code" ;;
  esac
}

setting() {  # value of a top-level key of the settings file (comment and quotes removed)
  sed -n "s/^$1:[[:space:]]*\([^#]*\).*/\1/p" "$SETTINGS" | head -n 1 | sed "s/[[:space:]]*\$//; s/^[\"']//; s/[\"']\$//"
}

has_value() {  # variable set in the environment, or a non-empty line NAME=value in .env
  local name="$1"
  [ -n "${!name:-}" ] && return 0
  [ -f .env ] && grep -qE "^[[:space:]]*(export[[:space:]]+)?$name[[:space:]]*=[[:space:]]*[^[:space:]#]" .env
}

[ -f "$SETTINGS" ] || stop "settings not found: $SETTINGS (copy deploy/updater/settings.updater.yaml to config/settings.yaml)" 2
[ -x "$PY" ] || command -v "$PY" >/dev/null 2>&1 || stop "Python not found ($PY): create .venv (deploy/onprem/README.md, step 10) or set TSI_PYTHON" 2

PROVIDER="$(setting llm_provider)"
PROVIDER="${PROVIDER:-claude_cli}"
if [ "$PROVIDER" = openai ]; then
  KEY="$(setting openai_api_key_env)"
  KEY="${KEY:-OPENAI_API_KEY}"
  has_value "$KEY" || stop "key $KEY is missing: set it in the environment or in $ROOT/.env" 2
  echo "LLM provider: openai ($(setting openai_model), $(setting openai_base_url))"
elif [ "$PROVIDER" = claude_cli ]; then
  command -v claude >/dev/null 2>&1 || stop "llm_provider is claude_cli but the 'claude' command is missing: use the GPT@EC settings (deploy/updater/settings.updater.yaml)" 2
  echo "LLM provider: claude_cli (Claude Code CLI)"
else
  echo "LLM provider: $PROVIDER"
fi

MONTH="${UPDATE_MONTH:-$(date +%-m)}"
if [ "$SCHEDULED" = 1 ]; then
  case "$MONTH" in
    1|4|7|10) RECHECK=1; echo "Scheduled run in January, April, July or October: quarterly full check (--recheck-all)." ;;
  esac
fi
[ "$RECHECK" = 1 ] && MODE="full check"
[ "$SCHEDULED" = 1 ] && MODE="$MODE (scheduled)"
echo "Mode: $MODE. Log: $ROOT/$LOG"

if [ "$SYNC" = 1 ] && ! has_value TSI_DATABASE_URL; then
  warn "TSI_DATABASE_URL missing (environment or .env): db-sync skipped, the database is not updated" 1
  SYNC=0
fi

RECHECK_ARGS=()
[ "$RECHECK" = 1 ] && RECHECK_ARGS=(--recheck-all)

llm_step run run "${RECHECK_ARGS[@]}"

if [ "$SKIP_DELIVERABLES" = 1 ]; then
  echo; echo "=== deliverables: skipped (--skip-deliverables)"
else
  llm_step deliverables deliverables "${RECHECK_ARGS[@]}"
fi

if [ "$SKIP_CSR" = 1 ]; then
  echo; echo "=== CSR: skipped (--skip-csr)"
else
  step csr-fetch soft csr-fetch \
    || warn "csr-fetch failed (exit code $?): ECFIN site unavailable? the previous CSR export is kept" 1
  # The Commission's recommendations of the current year are fetched once published (from May-June).
  step csr-build strict csr-build --fetch-missing
  code=$?
  [ "$code" = 0 ] || stop "csr-build failed (exit code $code)" "$code"
  step country-reports soft country-reports \
    || warn "country-reports: some reports could not be downloaded (exit code $?); the others are kept" 1
  LINK_ARGS=(csr-link)
  [ "$DETAIL_ONLY" = 1 ] && LINK_ARGS+=(--detail-only)
  llm_step csr-link "${LINK_ARGS[@]}"
fi

step anomalies strict anomalies
code=$?
[ "$code" = 0 ] || stop "anomalies failed (exit code $code)" "$code"

if [ "$SYNC" = 1 ]; then
  if ! step db-sync soft db-sync; then
    wait_s="${UPDATE_RETRY_WAIT_S:-60}"
    echo "db-sync failed: new attempt in $wait_s s (the database was not changed)"
    sleep "$wait_s"
    step db-sync soft db-sync \
      || warn "db-sync failed twice (exit code $?): the database keeps its previous data; check the ERREUR message, then run: python -m tsi_agent db-sync" 1
  fi
fi

if [ "$PROD_EXPORT" = 1 ]; then
  step prod-export strict prod-export
  code=$?
  [ "$code" = 0 ] || stop "prod-export failed (exit code $code)" "$code"
fi

finish "$WORST"
