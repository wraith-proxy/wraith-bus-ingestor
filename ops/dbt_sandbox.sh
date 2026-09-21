#!/usr/bin/env bash
# Make a throwaway copy of the dbt project to learn and experiment in.
#
#   ops/dbt_sandbox.sh [DIR]        (default DIR: ~/dbt-sandbox)
#
# WHY: the real dbt/ directory belongs to the nightly build. Running `dbt`
# there writes into wraith_bus_ingestor_build.duckdb, the file the 03:00 run
# depends on, and a stray connection can make that run fail. The sandbox has its
# OWN warehouse and a SMALL slice of the data (three parquet files and NaPTAN),
# so every command in docs/learn-dbt/ runs in seconds and cannot hurt anything.
#
# Safe to re-run: it deletes and recreates DIR (and only DIR).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/dbt-sandbox}"
FILES="${SANDBOX_PARQUET_FILES:-3}"

case "$DEST" in
  "$REPO"|"$REPO"/*) echo "!! refusing: DIR must be OUTSIDE the repo ($REPO)" >&2; exit 1 ;;
  ""|"/"|"$HOME") echo "!! refusing: DIR is '$DEST'" >&2; exit 1 ;;
esac
[[ -x "$REPO/.venv/bin/dbt" ]] || { echo "!! no $REPO/.venv/bin/dbt -- run 'uv sync' in the repo first" >&2; exit 1; }
[[ -f "$REPO/dbt/data/naptan_stops.csv" ]] || { echo "!! no dbt/data/naptan_stops.csv -- has the nightly run yet?" >&2; exit 1; }

rm -rf "$DEST"
mkdir -p "$DEST/data"

# the project itself: models, macros, tests, config -- but no warehouses,
# logs, old build output or data
rsync -a \
  --exclude='*.duckdb' --exclude='*.duckdb.wal' --exclude='data/' --exclude='target/' \
  --exclude='logs/' --exclude='__pycache__' --exclude='*.log' \
  "$REPO/dbt/" "$DEST/"

# a small slice of real data
cp "$REPO/dbt/data/naptan_stops.csv" "$DEST/data/"
ls "$REPO"/dbt/data/bus_positions_*.parquet | sort | tail -n "$FILES" | while read -r f; do
  cp "$f" "$DEST/data/"
done

# `source sandbox.env` puts the project's dbt on your PATH and points dbt at
# this folder's profiles.yml, so a bare `dbt run` works from anywhere inside it.
cat > "$DEST/sandbox.env" <<EOF
export PATH="$REPO/.venv/bin:\$PATH"
export DBT_PROFILES_DIR="$DEST"
cd "$DEST"
echo "sandbox ready: \$(pwd)   (dbt: \$(command -v dbt))"
EOF

echo "sandbox created at $DEST"
echo "  $(ls "$DEST"/data/bus_positions_*.parquet | wc -l) parquet files + naptan_stops.csv, $(du -sh "$DEST" | cut -f1) total"
echo
echo "next:   source $DEST/sandbox.env"
