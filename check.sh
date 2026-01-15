#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
CHECK="$ROOT/check.py"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: .venv python not found at $PYTHON" >&2
  exit 1
fi

if [[ ! -f "$CHECK" ]]; then
  echo "error: check.py not found at $CHECK" >&2
  exit 1
fi

exec "$PYTHON" "$CHECK" "$@"
