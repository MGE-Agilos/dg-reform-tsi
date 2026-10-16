#!/usr/bin/env bash
# One step of the monthly update (.github/workflows/update-data.yml).
#
#   bash .github/scripts/run_step.sh <name> <policy> <command...>
#
# Records "<name> <exit code> <duration s>" in $CI_WORK/steps.tsv and copies stdout to $CI_WORK/<name>.out.
# Policies:
#   quota   exit 1 = LLM quota: warning, the next steps run, the job fails at the end; any other error fails now
#   soft    any error: warning, the next steps run, the job fails at the end
#   strict  any error fails now
# Deferred failures are listed in DEFERRED_FAILURES (GITHUB_ENV), checked by the last step of the job.
set -u
name=$1
policy=$2
shift 2
work=${CI_WORK:-ci-work}
mkdir -p "$work"
start=$(date +%s)
"$@" | tee "$work/$name.out"
code=${PIPESTATUS[0]}
printf '%s\t%s\t%s\n' "$name" "$code" "$(( $(date +%s) - start ))" >> "$work/steps.tsv"
if [ "$code" -eq 0 ]; then
  exit 0
fi
defer() {
  echo "::warning::$1"
  echo "DEFERRED_FAILURES=${DEFERRED_FAILURES:+$DEFERRED_FAILURES, }$name (exit $code)" >> "$GITHUB_ENV"
  exit 0
}
if [ "$policy" = quota ] && [ "$code" -eq 1 ]; then
  defer "$name stopped on the LLM quota (exit code 1): continuing, the job will fail at the end"
fi
if [ "$policy" = soft ]; then
  defer "$name failed (exit code $code): continuing with the previous data, the job will fail at the end"
fi
echo "::error::$name failed (exit code $code): see the log above and the logs artifact"
exit "$code"
