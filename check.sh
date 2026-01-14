#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-.}"

if [[ "$TARGET" == -* ]]; then
  echo "error: target must be a path, not an option: $TARGET" >&2
  exit 1
fi

cd "$ROOT"

if [[ "$TARGET" != "." && ! -e "$TARGET" ]]; then
  echo "error: target not found: $TARGET" >&2
  exit 1
fi

VENV="$ROOT/.venv"
PYTHON="$VENV/bin/python"
BIN="$VENV/bin"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: .venv python not found at $PYTHON" >&2
  exit 1
fi

STATUS=0
EXCLUDE_CSV=".venv,.git,__pycache__,.mypy_cache,.pytest_cache,data,datasets"

# ---- knobs ----
RUN_COVERAGE="${RUN_COVERAGE:-1}"                 # 1 to run coverage+pytest when possible, 0 to run plain pytest
COVERAGE_FAIL_UNDER="${COVERAGE_FAIL_UNDER:-80}"  # coverage minimum %
RUN_DIFF_COVER="${RUN_DIFF_COVER:-1}"             # 1 to run diff-cover when installed and repo looks sane
KEEP_LOGS="${KEEP_LOGS:-0}"                       # 1 to keep per-tool logs even on success
PRESERVE_COLOR="${PRESERVE_COLOR:-1}"             # 1 = keep colors via PTY, 0 = plain pipe/tee
KEEP_ANSI_LOGS="${KEEP_ANSI_LOGS:-0}"             # 0 = strip ANSI from logs (recommended), 1 = keep ANSI in logs

LOG_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t qa)"
mkdir -p "$LOG_DIR"

declare -a SUMMARY_LABELS=()
declare -a SUMMARY_STATE=()
declare -a SUMMARY_RC=()
declare -a SUMMARY_ERRORS=()
declare -a SUMMARY_WARNINGS=()
declare -a SUMMARY_LOGS=()

sanitize_label() {
  echo "$1" | tr ' /' '__' | tr -c 'A-Za-z0-9._-' '_'
}

add_summary() {
  local label="$1" state="$2" rc="$3" errors="$4" warnings="$5" log="$6"
  SUMMARY_LABELS+=("$label")
  SUMMARY_STATE+=("$state")
  SUMMARY_RC+=("$rc")
  SUMMARY_ERRORS+=("$errors")
  SUMMARY_WARNINGS+=("$warnings")
  SUMMARY_LOGS+=("$log")
}

run_with_pty() {
  local log="$1"
  shift

  "$PYTHON" - "$log" "$KEEP_ANSI_LOGS" "$@" <<'PY'
import os, pty, re, select, subprocess, sys

log_path = sys.argv[1]
keep_ansi = sys.argv[2] == "1"
cmd = sys.argv[3:]

ansi_re = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
os.close(slave_fd)

def write_log(fh, chunk: bytes) -> None:
  if keep_ansi:
    fh.write(chunk)
  else:
    fh.write(ansi_re.sub(b"", chunk))

with open(log_path, "wb") as f:
  try:
    while True:
      r, _, _ = select.select([master_fd], [], [], 0.05)
      if master_fd in r:
        try:
          data = os.read(master_fd, 4096)
        except OSError:
          data = b""
        if data:
          sys.stdout.buffer.write(data)
          sys.stdout.buffer.flush()
          write_log(f, data)
          f.flush()
          continue
      if proc.poll() is not None:
        # Drain remaining output
        while True:
          r2, _, _ = select.select([master_fd], [], [], 0)
          if master_fd not in r2:
            break
          data = os.read(master_fd, 4096)
          if not data:
            break
          sys.stdout.buffer.write(data)
          sys.stdout.buffer.flush()
          write_log(f, data)
        break
  finally:
    try:
      os.close(master_fd)
    except OSError:
      pass

sys.exit(proc.wait())
PY
}

extract_counts() {
  local label="$1"
  local log="$2"

  local errors=0
  local warnings=0

  case "$label" in
    "ruff")
      # Prefer Ruff's own summary, e.g. "Found 98 errors."
      local n
      n="$(grep -Eo 'Found [0-9]+ errors?' "$log" 2>/dev/null | tail -n1 | awk '{print $2}' || true)"
      if [[ -n "${n:-}" ]]; then
        errors="$n"
      else
        # Fallback: count diagnostic codes like I001, UP035, B904, etc.
        errors="$(grep -E '^[A-Z]{1,4}[0-9]{3,4}\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      fi
      ;;
    "black --check")
      local n
      n="$(grep -Eo '[0-9]+ file(s)? would be reformatted' "$log" 2>/dev/null | head -n1 | awk '{print $1}' || true)"
      if [[ -n "${n:-}" ]]; then
        errors="$n"
      else
        errors="$(grep -E 'would reformat ' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      fi
      ;;
    "mypy")
      local n
      n="$(grep -Eo 'Found [0-9]+ errors?' "$log" 2>/dev/null | tail -n1 | awk '{print $2}' || true)"
      if [[ -n "${n:-}" ]]; then
        errors="$n"
      else
        errors="$(grep -E '^[^:]+:[0-9]+: error:' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      fi
      ;;
    "pyright"|"basedpyright")
      # Typical: "38 errors, 0 warnings, 0 informations"
      local s
      s="$(grep -Eo '[0-9]+ errors?, [0-9]+ warnings?' "$log" 2>/dev/null | tail -n1 || true)"
      if [[ -n "${s:-}" ]]; then
        errors="$(echo "$s" | awk -F'[ ,]+' '{print $1}')"
        warnings="$(echo "$s" | awk -F'[ ,]+' '{print $3}')"
      else
        errors="$(grep -iE '\berror\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
        warnings="$(grep -iE '\bwarning\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      fi
      ;;
    "pytest"|"coverage run (pytest)")
      local summary
      summary="$(grep -E '={2,} .* in [0-9.]+s ={2,}' "$log" 2>/dev/null | tail -n1 || true)"
      if [[ -n "${summary:-}" ]]; then
        local fails errs warns
        fails="$(echo "$summary" | grep -Eo '[0-9]+ failed' | awk '{sum+=$1} END{print sum+0}')"
        errs="$(echo "$summary"  | grep -Eo '[0-9]+ error(s)?' | awk '{sum+=$1} END{print sum+0}')"
        warns="$(echo "$summary" | grep -Eo '[0-9]+ warnings?' | awk '{sum+=$1} END{print sum+0}')"
        errors="$((fails + errs))"
        warnings="$warns"
      else
        errors="$(grep -iE '\bfailed\b|\berror\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
        warnings="$(grep -iE '\bwarning\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      fi
      ;;
    "unittest")
      local f e
      f="$(grep -Eo 'failures=[0-9]+' "$log" 2>/dev/null | tail -n1 | cut -d= -f2 || true)"
      e="$(grep -Eo 'errors=[0-9]+' "$log" 2>/dev/null | tail -n1 | cut -d= -f2 || true)"
      f="${f:-0}"
      e="${e:-0}"
      errors="$((f + e))"
      ;;
    "deptry")
      local n
      n="$(grep -Eo 'Found [0-9]+ dependency issue(s)?' "$log" 2>/dev/null | tail -n1 | awk '{print $2}' || true)"
      if [[ -z "${n:-}" ]]; then
        n="$(grep -Eo 'Found [0-9]+ issue(s)?' "$log" 2>/dev/null | tail -n1 | awk '{print $2}' || true)"
      fi
      errors="${n:-0}"
      ;;
    "vulture")
      warnings="$(grep -E '^[^:]+:[0-9]+: ' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      ;;
    "semgrep")
      local n
      n="$(grep -Eo 'Ran [0-9]+ rules on [0-9]+ files: [0-9]+ findings' "$log" 2>/dev/null | tail -n1 | awk '{print $NF}' || true)"
      if [[ -n "${n:-}" && "$n" =~ ^[0-9]+$ ]]; then
        errors="$n"
      else
        # fallback: semgrep findings lines are not stable; use summary if present
        n="$(grep -Eo '[0-9]+ findings?' "$log" 2>/dev/null | tail -n1 | awk '{print $1}' || true)"
        errors="${n:-0}"
      fi
      ;;
    "bandit")
      local n
      n="$(grep -Eo 'Total issues: [0-9]+' "$log" 2>/dev/null | tail -n1 | awk '{print $3}' || true)"
      errors="${n:-0}"
      ;;
    "pip-audit")
      local n
      n="$(grep -Eo 'Found [0-9]+ vulnerabilities?' "$log" 2>/dev/null | tail -n1 | awk '{print $2}' || true)"
      if [[ -n "${n:-}" ]]; then
        errors="$n"
      else
        errors="$(awk 'BEGIN{n=0} /^[[:alnum:]_.-]+[[:space:]]+[0-9]/ {n++} END{print n}' "$log" 2>/dev/null || echo 0)"
      fi
      ;;
    "diff-cover")
      # diff-cover output formats vary. Best cheap heuristic: count "Missing" lines.
      warnings="$(grep -iE '\bmissing\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      ;;
    "coverage report")
      # coverage prints "TOTAL ... XX%"
      local pct
      pct="$(grep -E 'TOTAL[[:space:]]+[0-9]+' "$log" 2>/dev/null | tail -n1 | awk '{print $(NF-1)}' | tr -d '%' || true)"
      if [[ -n "${pct:-}" && "$pct" =~ ^[0-9]+$ ]]; then
        if (( pct < COVERAGE_FAIL_UNDER )); then
          errors=1
        fi
      fi
      ;;
    *)
      errors="$(grep -iE '\berror\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      warnings="$(grep -iE '\bwarning\b' "$log" 2>/dev/null | wc -l | tr -d ' ')"
      ;;
  esac

  errors="${errors:-0}"
  warnings="${warnings:-0}"
  if ! [[ "$errors" =~ ^[0-9]+$ ]]; then errors=0; fi
  if ! [[ "$warnings" =~ ^[0-9]+$ ]]; then warnings=0; fi

  echo "$errors $warnings"
}

run_cmd() {
  local label="$1"
  shift

  local safe
  safe="$(sanitize_label "$label")"
  local log="$LOG_DIR/${safe}.log"

  echo
  echo "==> $label"

  local rc=0
  if [[ "$PRESERVE_COLOR" == "1" ]]; then
    run_with_pty "$log" "$@"
    rc=$?
  else
    "$@" 2>&1 | tee "$log"
    rc="${PIPESTATUS[0]}"
  fi

  local errors warnings
  read -r errors warnings < <(extract_counts "$label" "$log")

  local state="PASS"
  if [[ "$rc" -ne 0 ]]; then
    state="FAIL"
    STATUS=1
  elif [[ "$errors" -gt 0 || "$warnings" -gt 0 ]]; then
    state="WARN"
  fi

  add_summary "$label" "$state" "$rc" "$errors" "$warnings" "$log"
  return "$rc"
}

missing() {
  local label="$1"
  local hint="$2"
  echo
  echo "==> $label (missing)"
  echo "$hint"
  STATUS=1
  add_summary "$label" "MISSING" "127" "0" "0" ""
}

skip() {
  local label="$1"
  local reason="$2"
  echo
  echo "==> $label (skipped - $reason)"
  add_summary "$label" "SKIP" "0" "0" "0" ""
}

run_tool() {
  local label="$1"
  local cmd="$2"
  local hint="$3"
  shift 3
  if [[ -x "$BIN/$cmd" ]]; then
    run_cmd "$label" "$BIN/$cmd" "$@"
  else
    missing "$label" "$hint"
  fi
}

print_summary() {
  echo
  echo "==> summary"

  local max_label=4
  local i
  for i in "${!SUMMARY_LABELS[@]}"; do
    local l="${SUMMARY_LABELS[$i]}"
    if (( ${#l} > max_label )); then
      max_label="${#l}"
    fi
  done

  local fmt="%-${max_label}s | %-7s | %3s | %6s | %8s\n"
  printf "$fmt" "tool" "result" "rc" "errors" "warnings"
  printf '%*s\n' "$((max_label + 35))" '' | tr ' ' '-'

  local total_errors=0
  local total_warnings=0
  local clean=0
  local total=0

  for i in "${!SUMMARY_LABELS[@]}"; do
    local label="${SUMMARY_LABELS[$i]}"
    local state="${SUMMARY_STATE[$i]}"
    local rc="${SUMMARY_RC[$i]}"
    local errors="${SUMMARY_ERRORS[$i]}"
    local warnings="${SUMMARY_WARNINGS[$i]}"

    total=$((total + 1))
    if [[ "$state" == "PASS" ]]; then
      clean=$((clean + 1))
    fi

    total_errors=$((total_errors + errors))
    total_warnings=$((total_warnings + warnings))

    printf "$fmt" "$label" "$state" "$rc" "$errors" "$warnings"
  done

  printf '%*s\n' "$((max_label + 35))" '' | tr ' ' '-'
  printf "$fmt" "TOTAL" "" "" "$total_errors" "$total_warnings"
  echo
  echo "clean: $clean/$total"
}

# ---- tools ----

# Force color where possible; PTY already helps, but this avoids some tool weirdness.
run_tool "ruff" "ruff" "Install: $PYTHON -m pip install ruff" check "$TARGET"
run_tool "black --check" "black" "Install: $PYTHON -m pip install black" --check "$TARGET"
run_tool "mypy" "mypy" "Install: $PYTHON -m pip install mypy" "$TARGET"

if [[ -x "$BIN/basedpyright" ]]; then
  run_cmd "basedpyright" "$BIN/basedpyright" --project "$ROOT"
elif [[ -x "$BIN/pyright" ]]; then
  run_cmd "pyright" "$BIN/pyright" --project "$ROOT"
else
  missing "pyright/basedpyright" "Install: $PYTHON -m pip install pyright"
fi

# ---- tests (+ coverage) ----
if [[ -x "$BIN/pytest" ]]; then
  if [[ "$RUN_COVERAGE" == "1" ]]; then
    if "$PYTHON" -m coverage --version >/dev/null 2>&1; then
      if run_cmd "coverage run (pytest)" "$PYTHON" -m coverage run -m pytest --color=yes; then
        run_cmd "coverage report" "$PYTHON" -m coverage report --fail-under="$COVERAGE_FAIL_UNDER" --show-missing
        run_cmd "coverage xml" "$PYTHON" -m coverage xml

        if [[ "$RUN_DIFF_COVER" == "1" ]]; then
          if [[ -x "$BIN/diff-cover" ]]; then
            if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
              compare_branch="${DIFF_COVER_COMPARE_BRANCH:-}"
              if [[ -z "$compare_branch" ]]; then
                if git show-ref --verify --quiet refs/remotes/origin/main; then
                  compare_branch="origin/main"
                elif git show-ref --verify --quiet refs/remotes/origin/master; then
                  compare_branch="origin/master"
                elif git show-ref --verify --quiet refs/heads/main; then
                  compare_branch="main"
                elif git show-ref --verify --quiet refs/heads/master; then
                  compare_branch="master"
                fi
              fi

              if [[ -n "${compare_branch:-}" ]]; then
                run_cmd "diff-cover" "$BIN/diff-cover" coverage.xml --fail-under="$COVERAGE_FAIL_UNDER" --compare-branch="$compare_branch"
              else
                skip "diff-cover" "no obvious main/master compare branch (set DIFF_COVER_COMPARE_BRANCH=...)"
              fi
            else
              skip "diff-cover" "not a git repo"
            fi
          else
            skip "diff-cover" "diff-cover not installed (pip install diff-cover)"
          fi
        else
          skip "diff-cover" "RUN_DIFF_COVER=0"
        fi
      else
        skip "coverage report" "pytest failed"
        skip "coverage xml" "pytest failed"
        skip "diff-cover" "pytest failed"
      fi
    else
      missing "coverage" "Install: $PYTHON -m pip install coverage"
      run_cmd "pytest" "$BIN/pytest" --color=yes
    fi
  else
    run_cmd "pytest" "$BIN/pytest" --color=yes
  fi

  if [[ "${RUN_UNITTEST:-0}" == "1" ]]; then
    run_cmd "unittest" "$PYTHON" -m unittest
  fi
else
  run_cmd "unittest" "$PYTHON" -m unittest
fi

if [[ -x "$BIN/deptry" ]]; then
  if grep -q '^\[project\]' pyproject.toml 2>/dev/null || compgen -G "requirements*.txt" > /dev/null; then
    run_cmd "deptry" "$BIN/deptry" .
  else
    skip "deptry" "no dependency metadata found"
  fi
else
  missing "deptry" "Install: $PYTHON -m pip install deptry"
fi

if [[ -x "$BIN/vulture" ]]; then
  run_cmd "vulture" "$BIN/vulture" "$TARGET" --exclude "$EXCLUDE_CSV"
else
  missing "vulture" "Install: $PYTHON -m pip install vulture"
fi

if [[ -x "$BIN/semgrep" ]]; then
  SEMGREP_CONFIG="${SEMGREP_CONFIG:-p/python}"
  run_cmd "semgrep" \
    "$BIN/semgrep" scan \
    --config "$SEMGREP_CONFIG" \
    --exclude ".venv" \
    --exclude ".git" \
    --exclude "__pycache__" \
    --exclude ".mypy_cache" \
    --exclude ".pytest_cache" \
    --exclude "data" \
    --exclude "datasets" \
    "$TARGET"
else
  missing "semgrep" "Install: $PYTHON -m pip install semgrep"
fi

if "$PYTHON" -m bandit --version >/dev/null 2>&1; then
  if [[ "${RUN_BANDIT:-0}" != "1" ]]; then
    skip "bandit" "set RUN_BANDIT=1 to enable"
  elif [[ -d "$TARGET" ]]; then
    run_cmd "bandit" "$PYTHON" -m bandit -r "$TARGET" -x "$EXCLUDE_CSV" -s B101
  else
    run_cmd "bandit" "$PYTHON" -m bandit "$TARGET" -s B101
  fi
else
  missing "bandit" "Install: $PYTHON -m pip install bandit"
fi

run_tool "pip-audit" "pip-audit" "Install: $PYTHON -m pip install pip-audit"

print_summary

if [[ "$STATUS" -ne 0 || "$KEEP_LOGS" == "1" ]]; then
  echo
  echo "logs: $LOG_DIR"
else
  rm -rf "$LOG_DIR"
fi

exit "$STATUS"
