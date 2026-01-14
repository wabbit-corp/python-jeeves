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

run() {
  local label="$1"
  shift
  echo
  echo "==> $label"
  "$@"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    STATUS=1
  fi
}

missing() {
  local label="$1"
  local hint="$2"
  echo
  echo "==> $label (missing)"
  echo "$hint"
  STATUS=1
}

skip() {
  local label="$1"
  local reason="$2"
  echo
  echo "==> $label (skipped - $reason)"
}

run_tool() {
  local label="$1"
  local cmd="$2"
  local hint="$3"
  shift 3
  if [[ -x "$BIN/$cmd" ]]; then
    run "$label" "$BIN/$cmd" "$@"
  else
    missing "$label" "$hint"
  fi
}

run_tool "ruff" "ruff" "Install: $PYTHON -m pip install ruff" check "$TARGET"
run_tool "black --check" "black" "Install: $PYTHON -m pip install black" --check "$TARGET"
run_tool "mypy" "mypy" "Install: $PYTHON -m pip install mypy" "$TARGET"

if [[ -x "$BIN/basedpyright" ]]; then
  run "basedpyright" "$BIN/basedpyright" --project "$ROOT"
elif [[ -x "$BIN/pyright" ]]; then
  run "pyright" "$BIN/pyright" --project "$ROOT"
else
  missing "pyright/basedpyright" "Install: $PYTHON -m pip install pyright"
fi

if [[ -x "$BIN/pytest" ]]; then
  run "pytest" "$BIN/pytest"
  if [[ "${RUN_UNITTEST:-0}" == "1" ]]; then
    run "unittest" "$PYTHON" -m unittest
  fi
else
  run "unittest" "$PYTHON" -m unittest
fi

if [[ -x "$BIN/deptry" ]]; then
  if grep -q '^\[project\]' pyproject.toml 2>/dev/null || compgen -G "requirements*.txt" > /dev/null; then
    run "deptry" "$BIN/deptry" .
  else
    skip "deptry" "no dependency metadata found"
  fi
else
  missing "deptry" "Install: $PYTHON -m pip install deptry"
fi

if [[ -x "$BIN/vulture" ]]; then
  run "vulture" "$BIN/vulture" "$TARGET" --exclude "$EXCLUDE_CSV"
else
  missing "vulture" "Install: $PYTHON -m pip install vulture"
fi

if [[ -x "$BIN/semgrep" ]]; then
  SEMGREP_CONFIG="${SEMGREP_CONFIG:-p/python}"
  run "semgrep" \
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
    run "bandit" "$PYTHON" -m bandit -r "$TARGET" -x "$EXCLUDE_CSV" -s B101
  else
    run "bandit" "$PYTHON" -m bandit "$TARGET" -s B101
  fi
else
  missing "bandit" "Install: $PYTHON -m pip install bandit"
fi

run_tool "pip-audit" "pip-audit" "Install: $PYTHON -m pip install pip-audit"

exit "$STATUS"
