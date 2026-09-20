#!/usr/bin/env bash
# Interactive pipeline: run each Python step, show where output went, then wait.
#
# Usage:
#   ./run_pipeline.sh <input.csv>
#
# After every step, type:
#   go next step
#       continue to the next file
#   rerun [extra arguments...]
#       run the same step again; extra arguments are appended to the defaults
#   rerun previous [extra arguments...]
#       go back one step and run that file (optional extra arguments)
#   rerun <n> [extra arguments...]
#       run step number n (1–5), including earlier files
#   rerun <name> [extra arguments...]
#       same, by short name (guide_anonymize, redacted_profile, privacy_review,
#       tests, linkage_analysis)
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

STEP_NAMES=(
  ""
  "guide_anonymize"
  "redacted_profile"
  "privacy_review"
  "tests"
  "linkage_analysis"
)
N_STEPS=5

LOG_DIR="outputs/pipeline_logs"
mkdir -p "$LOG_DIR" outputs

usage() {
  echo "Usage: $0 <input.csv>" >&2
  echo "  After each step: 'go next step'" >&2
  echo "                   'rerun [args]'                 (this step)" >&2
  echo "                   'rerun previous [args]'        (previous file)" >&2
  echo "                   'rerun <n|name> [args]'        (any step 1–5)" >&2
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
    echo "Finished with exit code $rc (you can rerun this or a previous step)."
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

show_artefacts() {
  local n="$1"
  echo "Expected artefacts:"
  case "$n" in
    1) list_existing "$ANON_CSV" "$ANON_REPORT" ;;
    2) list_existing "$PROFILE_JSON" ;;
    3) list_existing "$REVIEW_JSON" "$REVIEW_MD" "outputs/privacy_review_calls.jsonl" ;;
    4) list_existing "$PRIVACY_REPORT" ;;
    5) list_existing "$LINKAGE_REPORT" "$LINKAGE_VIOLATIONS" ;;
  esac
}

resolve_step() {
  local token="$1"
  local i
  case "$token" in
    [1-5])
      echo "$token"
      return 0
      ;;
  esac
  for i in $(seq 1 "$N_STEPS"); do
    if [[ "${STEP_NAMES[$i]}" == "$token" ]]; then
      echo "$i"
      return 0
    fi
  done
  return 1
}

run_step() {
  local n="$1"
  shift
  local -a extra=("$@")
  local name="${STEP_NAMES[$n]}"
  local log="$LOG_DIR/step${n}_${name}.log"
  local -a cmd

  case "$n" in
    1) cmd=("$PYTHON" guide_anonymize.py "$INPUT_CSV" "$ANON_NAME") ;;
    2) cmd=("$PYTHON" redacted_profile.py "$ANON_CSV" --subject release_candidate) ;;
    3) cmd=("$PYTHON" privacy_review.py request "$PROFILE_JSON" -o "$REVIEW_JSON") ;;
    4) cmd=("$PYTHON" tests.py "$ANON_CSV" --out "$PRIVACY_REPORT") ;;
    5) cmd=("$PYTHON" src/linkage_analysis.py --input "$ANON_CSV" --out "$LINKAGE_REPORT" --violations-out "$LINKAGE_VIOLATIONS") ;;
    *)
      echo "Unknown step $n" >&2
      return 1
      ;;
  esac

  echo "============================================================"
  echo "Step $n: $name"
  echo "============================================================"

  if [[ ${#extra[@]} -gt 0 ]]; then
    run_cmd "$log" "${cmd[@]}" "${extra[@]}"
  else
    run_cmd "$log" "${cmd[@]}"
  fi

  echo
  echo "It ran: $name (step $n)"
  echo "Captured stdout/stderr: $ROOT/$log"
  show_artefacts "$n"
}

prompt_after_step() {
  # stdout: next | quit | rerun <n> [args...]
  local current="$1"
  local line first rest target
  while true; do
    echo >&2
    echo "You are after step $current (${STEP_NAMES[$current]})." >&2
    if [[ "$current" -gt 1 ]]; then
      echo "Previous files: $(seq -s ', ' 1 "$((current - 1))") — or names: ${STEP_NAMES[*]:1:$((current - 1))}" >&2
    fi
    echo "Type:" >&2
    echo "  go next step" >&2
    echo "  rerun [extra args]                 (this file again)" >&2
    if [[ "$current" -gt 1 ]]; then
      echo "  rerun previous [extra args]        (previous file)" >&2
      echo "  rerun <n|name> [extra args]        (any earlier or current file)" >&2
    fi
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
        echo "rerun $current"
        return 0
        ;;
      rerun\ *)
        rest="${line#rerun }"
        first="${rest%% *}"
        if [[ "$first" == "previous" || "$first" == "prev" || "$first" == "back" ]]; then
          if [[ "$current" -le 1 ]]; then
            echo "There is no previous step." >&2
            continue
          fi
          if [[ "$rest" == "$first" ]]; then
            echo "rerun $((current - 1))"
          else
            echo "rerun $((current - 1)) ${rest#* }"
          fi
          return 0
        fi
        if target="$(resolve_step "$first")"; then
          if [[ "$target" -gt "$current" ]]; then
            echo "Step $target has not been reached yet. Stay on 1–$current." >&2
            continue
          fi
          if [[ "$rest" == "$first" ]]; then
            echo "rerun $target"
          else
            echo "rerun $target ${rest#* }"
          fi
          return 0
        fi
        echo "rerun $current $rest"
        return 0
        ;;
      *)
        echo "Not recognised. Examples: go next step | rerun --k-min 20 | rerun previous | rerun 1 --k-min 5" >&2
        ;;
    esac
  done
}

current=1
extra=()

while [[ "$current" -le "$N_STEPS" ]]; do
  run_step "$current" "${extra[@]+"${extra[@]}"}"
  extra=()

  reply="$(prompt_after_step "$current")"
  case "$reply" in
    next)
      current=$((current + 1))
      ;;
    quit)
      echo "Stopped after step $((current))."
      exit 0
      ;;
    rerun\ *)
      rest="${reply#rerun }"
      first="${rest%% *}"
      current="$first"
      if [[ "$rest" == "$first" ]]; then
        extra=()
        echo "Rerunning step $current (${STEP_NAMES[$current]}) with default arguments."
      else
        # shellcheck disable=SC2206
        extra=(${rest#* })
        echo "Rerunning step $current (${STEP_NAMES[$current]}) with extra arguments: ${extra[*]}"
      fi
      ;;
  esac
done

echo
echo "All five steps completed."
echo "Logs are under $ROOT/$LOG_DIR/"
