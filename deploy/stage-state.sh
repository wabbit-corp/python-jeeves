#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${1:-$ROOT/deploy-artifacts/state/$STAMP}"
SQLITE_DIR="$OUT_DIR/sqlite"
PYTHON="${JEEVES_PYTHON:-$ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
  echo "A runtime Python with sqlcipher3 is required; set JEEVES_PYTHON if needed." >&2
  exit 1
fi

umask 077
mkdir -p "$SQLITE_DIR"
cd "$ROOT"

DBS=(
  "servant_index.sqlite3"
  "servant_commitments.sqlite3"
  "servant_event_channels.sqlite3"
  "servant_topic_subscriptions.sqlite3"
)

for db in "${DBS[@]}"; do
  src="$ROOT/$db"
  dest="$SQLITE_DIR/$db"
  if [[ ! -f "$src" ]]; then
    echo "Skipping missing database: $db"
    continue
  fi
  echo "Backing up $db -> $dest"
  "$PYTHON" -m servant.scripts.encrypt_databases --encrypted-source "$src" "$dest"
done

echo "Staged deployment state in: $OUT_DIR"
