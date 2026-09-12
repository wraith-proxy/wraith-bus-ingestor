#!/usr/bin/env bash
#
# One-shot rebuild: pull fresh data out of QuestDB, build every model, assert
# the results, and publish the marts for the API to read.
#
#   ./build.sh                  extract, build, test, publish
#   SKIP_EXTRACT=1 ./build.sh   reuse whatever is already in data/
#   KEEP_BUILD=1   ./build.sh   keep the scratch warehouse for debugging
#
# TWO THINGS THIS SCRIPT IS CAREFUL ABOUT, both learned the hard way:
#
#  1. It never lets dbt touch the file Flask is serving. DuckDB holds an
#     exclusive lock for the whole run, so building in place made every
#     read-only open fail and put all ~4200 buses on the map into 'unknown'
#     for the duration. dbt builds into ghost_bus_build.duckdb; the marts are
#     copied into a new file and moved over ghost_bus.duckdb with a rename,
#     which is atomic within a filesystem. Readers see the whole old file or
#     the whole new one, never a lock or a half-written table.
#
#  2. It publishes ONLY after the tests pass. A build that fails its tests
#     leaves the previous good marts serving rather than replacing them with
#     something known to be wrong.
#
# It also runs the whole thing at low priority: DuckDB will happily take every
# core on the box, and the API shares it.
#
set -euo pipefail

cd "$(dirname "$0")"          # every path below -- and every source in
                              # _sources.yml -- is relative to dbt/

PY=".venv/bin/python"
: "${QUESTDB_HTTP:=http://localhost:9001}"
: "${EXTRACT_DAYS:=22}"
: "${SKIP_EXTRACT:=0}"
: "${KEEP_BUILD:=0}"

export QUESTDB_HTTP EXTRACT_DAYS

SERVED="ghost_bus.duckdb"
BUILD="ghost_bus_build.duckdb"
STAGED="ghost_bus_staged.duckdb"

MARTS=(mart_eta_model mart_localities mart_line_localities mart_line_stops mart_line_punctuality)

# Low priority for everything heavy. `nice` is always present; `ionice` is not
# (it needs util-linux and a supporting scheduler), so fall back gracefully.
NICE=(nice -n 15)
if command -v ionice >/dev/null 2>&1 && ionice -c3 true >/dev/null 2>&1; then
    NICE=(nice -n 15 ionice -c3)
fi
DBT=("${NICE[@]}" .venv/bin/dbt)

started=$(date +%s)

if [[ "$SKIP_EXTRACT" == "1" ]]; then
    echo "==> skipping extract (SKIP_EXTRACT=1)"
else
    echo "==> extracting ${EXTRACT_DAYS} days from ${QUESTDB_HTTP}"
    "${NICE[@]}" "$PY" extract.py
fi

if [[ ! -f data/naptan_stops.csv ]]; then
    echo "!! data/naptan_stops.csv is missing -- the NaPTAN register is not" >&2
    echo "   downloaded by this script. Fetch it before building." >&2
    exit 1
fi

# The extract writes no file at all for a day the feed was down, so the date
# range has holes. That is expected and handled (int_bus_journeys breaks a
# journey on any gap over 20 min; int_journey_shape discards journeys whose end
# falls in a hole) -- but it is worth saying out loud how much data there is.
echo "==> $(ls data/bus_positions_*.parquet 2>/dev/null | wc -l) daily parquet files in data/"

# No packages.yml: this project deliberately has no package dependencies, so
# there is nothing for `dbt deps` to do. The guard is here so that adding one
# later does not silently break the build.
if [[ -f packages.yml ]]; then
    echo "==> dbt deps"
    "${DBT[@]}" deps --profiles-dir .
fi

# Start the scratch warehouse from scratch. A stale write-ahead log from a
# killed run will otherwise be replayed into the new build.
rm -f "$BUILD" "$BUILD.wal" "$STAGED" "$STAGED.wal"

echo "==> dbt run   (into $BUILD, at low priority)"
"${DBT[@]}" run --profiles-dir .

echo "==> dbt test"
"${DBT[@]}" test --profiles-dir .

# ---------------------------------------------------------------------------
# Publish. Only the five marts are copied: the API reads nothing else, and the
# materialised intermediates are ~95% of the scratch file's size. Copying into
# a fresh database rather than deleting from a copy of the old one is what
# actually reclaims the space -- DuckDB does not shrink a file on DROP.
# ---------------------------------------------------------------------------
echo "==> publishing marts"
"${NICE[@]}" "$PY" - "$BUILD" "$STAGED" "${MARTS[@]}" <<'PYEOF'
import sys

import duckdb

build, staged, *marts = sys.argv[1:]

con = duckdb.connect(build, read_only=True)
# ATTACH inherits the parent connection's access mode by default, so without
# this the staged file -- which does not exist yet -- fails to open read-only.
con.execute(f"attach '{staged}' as published (read_only false)")
for mart in marts:
    con.execute(f"create table published.{mart} as select * from {mart}")
    n = con.execute(f"select count(*) from published.{mart}").fetchone()[0]
    print(f"  {mart:<22} {n:>9,}")
con.execute("detach published")
con.close()
PYEOF

# Atomic within a filesystem: readers never see a partial file. The old .wal
# must go with the old file, or DuckDB will try to replay it against the new one.
mv -f "$STAGED" "$SERVED"
rm -f "$SERVED.wal"

if [[ "$KEEP_BUILD" == "1" ]]; then
    echo "==> keeping $BUILD (KEEP_BUILD=1)"
else
    rm -f "$BUILD" "$BUILD.wal"
fi

elapsed=$(( $(date +%s) - started ))
echo
echo "==> published $SERVED ($(du -h "$SERVED" | cut -f1)) in $((elapsed / 60))m $((elapsed % 60))s"
echo "    the API can open it read-only; it was never locked during the build."
