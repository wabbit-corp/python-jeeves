#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${1:-$ROOT/deploy-artifacts/state/$STAMP}"
SQLITE_DIR="$OUT_DIR/sqlite"
MODEL_SRC="$ROOT/codi/api/training/tmp/models"
MODEL_DEST="$OUT_DIR/codi-models"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required to stage deployment state." >&2
  exit 1
fi

mkdir -p "$SQLITE_DIR"

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
  sqlite3 "$src" ".backup '$dest'"
  result="$(sqlite3 "$dest" 'PRAGMA integrity_check;')"
  if [[ "$result" != "ok" ]]; then
    echo "Integrity check failed for $db: $result" >&2
    exit 1
  fi
done

if [[ -d "$MODEL_SRC" ]]; then
  mkdir -p "$MODEL_DEST"
  rsync -a "$MODEL_SRC/" "$MODEL_DEST/"
fi

echo "Staged deployment state in: $OUT_DIR"
