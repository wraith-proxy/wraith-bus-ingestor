#!/usr/bin/env bash
# Make a throwaway copy of the dbt project to learn and experiment in.
#
#   ops/dbt_sandbox.sh [DIR]        (default DIR: ~/dbt-sandbox)
#
# WHY: the real dbt/ directory belongs to the nightly build. Running `dbt`
# there writes into wraith_bus_ingestor_build.duckdb, the file the 03:00 run
# depends on, and a stray connection can make that run fail. The sandbox has
# its OWN warehouse and OWN data, so every command in docs/learn-dbt/ runs in
# seconds and cannot hurt anything.
#
# The data is SYNTHETIC, not a slice of the real feed. Real bus positions live
# in QuestDB and are gitignored on purpose -- nothing gets committed that
# isn't meant to be public, and a fresh clone of this repo has none of it. So
# this script never touches dbt/data/; it builds the sandbox's data from two
# small fixtures under dbt/tests/fixtures/, which ARE committed:
#   - sandbox_naptan_stops.csv        a still image, six fictional towns
#   - generate_sandbox_positions.py   a few journeys, expanded to fresh dates
#     every time this script runs, so the source freshness check always passes
#
# Safe to re-run: it deletes and recreates DIR (and only DIR).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/dbt-sandbox}"

case "$DEST" in
  "$REPO"|"$REPO"/*) echo "!! refusing: DIR must be OUTSIDE the repo ($REPO)" >&2; exit 1 ;;
  ""|"/"|"$HOME") echo "!! refusing: DIR is '$DEST'" >&2; exit 1 ;;
esac
[[ -x "$REPO/.venv/bin/dbt" ]] || { echo "!! no $REPO/.venv/bin/dbt -- run 'uv sync' in the repo first" >&2; exit 1; }

FIXTURES="$REPO/dbt/tests/fixtures"
[[ -f "$FIXTURES/sandbox_naptan_stops.csv" ]] || { echo "!! missing $FIXTURES/sandbox_naptan_stops.csv" >&2; exit 1; }

rm -rf "$DEST"
mkdir -p "$DEST/data"

# the project itself: models, macros, tests, config -- but no warehouses,
# logs, old build output or (real, gitignored) data
rsync -a \
  --exclude='*.duckdb' --exclude='*.duckdb.wal' --exclude='data/' --exclude='target/' \
  --exclude='logs/' --exclude='__pycache__' --exclude='*.log' \
  "$REPO/dbt/" "$DEST/"

# the fixtures, turned into a sandbox's worth of data
cp "$FIXTURES/sandbox_naptan_stops.csv" "$DEST/data/naptan_stops.csv"
"$REPO/.venv/bin/python" "$FIXTURES/generate_sandbox_positions.py" \
  "$DEST/data/bus_positions_sandbox.parquet"

# One real test does not fit any small fixture: the bus_positions source
# requires at least 500,000 rows (a floor for "did a whole-GB extract produce
# basically nothing", not for a teaching sandbox of a few hundred). Lowered
# here, in the SANDBOX'S OWN COPY only -- never in the repo -- to a number our
# tiny fixture can actually clear. Every other one of the project's 88 tests
# runs unmodified.
SRC="$DEST/models/staging/bods/_bods__sources.yml"
if grep -q 'min_value: 500000' "$SRC"; then
  sed -i 's/min_value: 500000/min_value: 40/' "$SRC"
else
  echo "!! expected 'min_value: 500000' in $SRC -- the real project's threshold" >&2
  echo "   may have moved; sandbox_naptan_stops fixture note in dbt_sandbox.sh needs a look" >&2
fi

# `source sandbox.env` puts the project's dbt on your PATH and points dbt at
# this folder's profiles.yml, so a bare `dbt run` works from anywhere inside it.
cat > "$DEST/sandbox.env" <<EOF
export PATH="$REPO/.venv/bin:\$PATH"
export DBT_PROFILES_DIR="$DEST"
cd "$DEST"
echo "sandbox ready: \$(pwd)   (dbt: \$(command -v dbt))"
EOF

echo "sandbox created at $DEST"
echo "  $(du -sh "$DEST" | cut -f1) total, synthetic data only (see the header of this script)"
echo
echo "next:   source $DEST/sandbox.env"
