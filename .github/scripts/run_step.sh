#!/usr/bin/env bash
# One step of the monthly update (.github/workflows/update-data.yml).
#
#   bash .github/scripts/run_step.sh <name> <policy> <command...>
#
# Records "<name> <exit code> <duration s> <policy>" in $CI_WORK/steps.tsv and copies stdout to $CI_WORK/<name>.out.
# Policies:
#   quota   exit 1 = LLM quota: warning, the next steps run, the job fails at the end; any other error fails now
#   soft    any error: warning, the next steps run, the job fails at the end
#   strict  any error fails now
# Exit code 3 (`run`, `deliverables`: the sitemap could not be read, the step finished without it) is deferred
# under the quota and soft policies: the next steps run on the data kept, the job fails at the end (GitHub
# notifies), never retried.
# Options (environment):
#   STEP_BUDGET_MIN     time budget: the command gets SIGINT after N minutes (killed 5 min later); the work
#                       already written is kept, and the step is deferred like a quota (exit code 124)
#   STEP_RETRY_AFTER_S  on failure, wait N seconds and run the command once more
# Deferred failures are listed in DEFERRED_FAILURES (GITHUB_ENV), checked by the last step of the job.
set -u
name=$1
policy=$2
shift 2
work=${CI_WORK:-ci-work}
mkdir -p "$work"
start=$(date +%s)

attempt() {
  if [ -n "${STEP_BUDGET_MIN:-}" ]; then
    timeout -s INT -k 5m "${STEP_BUDGET_MIN}m" "$@" | tee "$work/$name.out"
  else
    "$@" | tee "$work/$name.out"
  fi
  return "${PIPESTATUS[0]}"
}

attempt "$@"
code=$?
if [ "$code" -ne 0 ] && [ "$code" -ne 124 ] && [ "$code" -ne 3 ] && [ -n "${STEP_RETRY_AFTER_S:-}" ]; then
  echo "::warning::$name failed (exit code $code): new attempt in ${STEP_RETRY_AFTER_S} s"
  sleep "$STEP_RETRY_AFTER_S"
  attempt "$@"
  code=$?
fi
printf '%s\t%s\t%s\t%s\n' "$name" "$code" "$(( $(date +%s) - start ))" "$policy" >> "$work/steps.tsv"
if [ "$code" -eq 0 ]; then
  exit 0
fi
defer() {
  echo "::warning::$1"
  echo "DEFERRED_FAILURES=${DEFERRED_FAILURES:+$DEFERRED_FAILURES, }$name (exit $code)" >> "$GITHUB_ENV"
  exit 0
}
if [ "$code" -eq 124 ] && [ "$policy" != strict ]; then
  defer "$name reached its time budget (${STEP_BUDGET_MIN:-?} min): continuing, the next run resumes; the job will fail at the end"
fi
if [ "$code" -eq 3 ] && [ "$policy" != strict ]; then
  defer "$name could not read the sitemap (exit code 3): known pages were not re-read and no date from an old copy was recorded; continuing, the job will fail at the end"
fi
if [ "$policy" = quota ] && [ "$code" -eq 1 ]; then
  defer "$name stopped on the LLM quota (exit code 1): continuing, the job will fail at the end"
fi
if [ "$policy" = soft ]; then
  defer "$name failed (exit code $code): continuing with the previous data, the job will fail at the end"
fi
echo "::error::$name failed (exit code $code): see the log above and the logs artifact"
exit "$code"
