#!/usr/bin/env bash
# Interactive pipeline: run each Python step, show where output went, then wait.
#
# Usage:
#   ./run_pipeline.sh <input.csv>
#
# After every step, type:
#   go next step
#       continue
#   rerun [extra arguments...]
#       run the same step again; extra arguments are appended to the defaults
#   quit
#       stop
#
# Optional environment:
#   PYTHON     interpreter (default: python3)
#   ANON_NAME  anonymised CSV filename under outputs/ (default: kanon.csv)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
ANON_NAME="${ANON_NAME:-kanon.csv}"
ANON_CSV="outputs/${ANON_NAME}"
ANON_REPORT="outputs/${ANON_NAME%.*}_report.json"
PROFILE_JSON="outputs/profile_release_candidate.json"
REVIEW_JSON="outputs/review_release_candidate.json"
REVIEW_MD="outputs/review_release_candidate.md"
PRIVACY_REPORT="outputs/privacy_report.json"
LINKAGE_REPORT="outputs/linkage_report.json"
LINKAGE_VIOLATIONS="outputs/linkage_violations.csv"

LOG_DIR="outputs/pipeline_logs"
mkdir -p "$LOG_DIR" outputs

usage() {
  echo "Usage: $0 <input.csv>" >&2
  echo "  After each step: 'go next step'  or  'rerun [extra args]'" >&2
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
fi
if [[ $# -lt 1 ]]; then
  usage
fi

INPUT_CSV="$1"
shift || true
if [[ $# -gt 0 ]]; then
  echo "Unexpected extra arguments: $*" >&2
  usage
fi
if [[ ! -f "$INPUT_CSV" ]]; then
  echo "Input CSV not found: $INPUT_CSV" >&2
  exit 1
fi

echo "Repo: $ROOT"
echo "Python: $PYTHON"
echo "Input: $INPUT_CSV"
echo
echo "Default chain:"
echo "  1. $PYTHON guide_anonymize.py $INPUT_CSV $ANON_NAME"
echo "     -> $ANON_CSV  and  $ANON_REPORT"
echo "  2. $PYTHON redacted_profile.py $ANON_CSV --subject release_candidate"
echo "     -> $PROFILE_JSON"
echo "  3. $PYTHON privacy_review.py request $PROFILE_JSON -o $REVIEW_JSON"
echo "     -> $REVIEW_JSON  and  $REVIEW_MD"
echo "     (needs PRIVACY_REVIEW_BASE_URL and PRIVACY_REVIEW_MODEL, or rerun with --offline-json)"
echo "  4. $PYTHON tests.py $ANON_CSV --out $PRIVACY_REPORT"
echo "     -> $PRIVACY_REPORT"
echo "  5. $PYTHON src/linkage_analysis.py --input $ANON_CSV --out $LINKAGE_REPORT --violations-out $LINKAGE_VIOLATIONS"
echo "     -> $LINKAGE_REPORT  and  $LINKAGE_VIOLATIONS"
echo

run_cmd() {
  local log="$1"
  shift
  echo "Command: $*"
  echo "Live log: $log"
  echo
  set +e
  "$@" 2>&1 | tee "$log"
  local rc=${PIPESTATUS[0]}
  set -e
  echo
  if [[ $rc -eq 0 ]]; then
    echo "Finished OK (exit 0)."
  else
    echo "Finished with exit code $rc (you can rerun this step)."
  fi
  return 0
}

list_existing() {
  local path
  for path in "$@"; do
    if [[ -e "$path" ]]; then
      echo "  exists: $path"
    else
      echo "  missing: $path"
    fi
  done
}

prompt_next_or_rerun() {
  # stdout is only: next | rerun | rerun <args> | quit
  local line extra
  while true; do
    echo >&2
    echo "Type 'go next step' to continue, or 'rerun' plus extra arguments to rerun this step." >&2
    echo -n "> " >&2
    IFS= read -r line || exit 1
    case "$line" in
      "go next step"|"go next"|"next")
        echo "next"
        return 0
        ;;
      quit|exit|q)
        echo "quit"
        return 0
        ;;
      rerun)
        echo "rerun"
        return 0
        ;;
      rerun\ *)
        extra="${line#rerun }"
        echo "rerun ${extra}"
        return 0
        ;;
      *)
        echo "Not recognised. Use exactly: go next step   or   rerun [args]" >&2
        ;;
    esac
  done
}

step_loop() {
  local n="$1"
  local name="$2"
  local log="$LOG_DIR/step${n}_${name}.log"
  shift 2
  local -a base_cmd=("$@")
  local -a extra=()
  local reply rest

  echo "============================================================"
  echo "Step $n: $name"
  echo "============================================================"

  while true; do
    if [[ ${#extra[@]} -gt 0 ]]; then
      run_cmd "$log" "${base_cmd[@]}" "${extra[@]}"
    else
      run_cmd "$log" "${base_cmd[@]}"
    fi
    extra=()

    echo
    echo "It ran: $name"
    echo "Captured stdout/stderr: $ROOT/$log"
    echo "Expected artefacts:"
    case "$n" in
      1) list_existing "$ANON_CSV" "$ANON_REPORT" ;;
      2) list_existing "$PROFILE_JSON" ;;
      3) list_existing "$REVIEW_JSON" "$REVIEW_MD" "outputs/privacy_review_calls.jsonl" ;;
      4) list_existing "$PRIVACY_REPORT" ;;
      5) list_existing "$LINKAGE_REPORT" "$LINKAGE_VIOLATIONS" ;;
    esac

    reply="$(prompt_next_or_rerun)"
    case "$reply" in
      next)
        return 0
        ;;
      quit)
        echo "Stopped after step $n."
        exit 0
        ;;
      rerun)
        echo "Rerunning step $n with the same default arguments."
        ;;
      rerun\ *)
        rest="${reply#rerun }"
        # shellcheck disable=SC2206
        extra=($rest)
        echo "Rerunning step $n with extra arguments: ${extra[*]}"
        ;;
    esac
  done
}

step_loop 1 guide_anonymize \
  "$PYTHON" guide_anonymize.py "$INPUT_CSV" "$ANON_NAME"

step_loop 2 redacted_profile \
  "$PYTHON" redacted_profile.py "$ANON_CSV" --subject release_candidate

step_loop 3 privacy_review \
  "$PYTHON" privacy_review.py request "$PROFILE_JSON" -o "$REVIEW_JSON"

step_loop 4 tests \
  "$PYTHON" tests.py "$ANON_CSV" --out "$PRIVACY_REPORT"

step_loop 5 linkage_analysis \
  "$PYTHON" src/linkage_analysis.py --input "$ANON_CSV" --out "$LINKAGE_REPORT" --violations-out "$LINKAGE_VIOLATIONS"

echo
echo "All five steps completed (or skipped via quit)."
echo "Logs are under $ROOT/$LOG_DIR/"
