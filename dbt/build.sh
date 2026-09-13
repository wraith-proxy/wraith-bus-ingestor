#!/usr/bin/env bash
#
# One-shot rebuild: pull fresh data out of QuestDB, build every model, assert
# the results, and publish the marts for the API to read.
#
#   ./build.sh                  extract, build, test, publish
#   SKIP_EXTRACT=1 ./build.sh   reuse whatever is already in data/
#   FULL_REFRESH=1 ./build.sh   wipe the scratch warehouse and rebuild every
#                               incremental model from scratch (schema change,
#                               suspected drift -- not needed routinely)
#
# THINGS THIS SCRIPT IS CAREFUL ABOUT, all learned the hard way:
#
#  1. It never lets dbt touch the file Flask is serving. DuckDB holds an
#     exclusive lock for the whole run, so building in place made every
#     read-only open fail and put all ~4200 buses on the map into 'unknown'
#     for the duration. dbt builds into wraith_bus_ingestor_build.duckdb; the marts are
#     copied into a new file and moved over wraith_bus_ingestor.duckdb with a rename,
#     which is atomic within a filesystem. Readers see the whole old file or
#     the whole new one, never a lock or a half-written table.
#
#  2. It publishes ONLY after the tests pass. A build that fails its tests
#     leaves the previous good marts serving rather than replacing them with
#     something known to be wrong.
#
#  3. wraith_bus_ingestor_build.duckdb now PERSISTS across runs by default. The two
#     most expensive models (int_pings_segmented_to_journeys,
#     int_pings_matched_to_stops) are incremental, and dbt's is_incremental()
#     only takes the fast path when the target table already exists -- if
#     this file never survived between runs, "incremental" would silently
#     mean "full rebuild every time"
#     anyway, which is exactly what happened until 2026-09-13 and is why
#     fixing one stale test used to cost a 20-30 min rebuild. FULL_REFRESH=1
#     is the deliberate escape hatch back to from-scratch.
#
# It also runs the whole thing at low priority: DuckDB will happily take every
# core on the box, and the API shares it.
#
set -euo pipefail

cd "$(dirname "$0")"          # every path below -- and every source in
                              # _sources.yml -- is relative to dbt/

# dbt used to keep its own separate .venv (a leftover from before this
# project moved under uv). One venv, not two: dbt-core and dbt-duckdb are
# real dependencies in the repo root's pyproject.toml now, so this just
# points at ../.venv, the same environment `uv sync` at the repo root
# already manages.
PY="../.venv/bin/python"
: "${QUESTDB_HTTP:=http://localhost:9001}"
: "${EXTRACT_DAYS:=22}"
: "${SKIP_EXTRACT:=0}"
: "${FULL_REFRESH:=0}"

export QUESTDB_HTTP EXTRACT_DAYS

SERVED="wraith_bus_ingestor.duckdb"
BUILD="wraith_bus_ingestor_build.duckdb"
STAGED="wraith_bus_ingestor_staged.duckdb"

MARTS=(eta_models localities line_localities line_stops line_punctuality)

# Low priority for everything heavy. `nice` is always present; `ionice` is not
# (it needs util-linux and a supporting scheduler), so fall back gracefully.
NICE=(nice -n 15)
if command -v ionice >/dev/null 2>&1 && ionice -c3 true >/dev/null 2>&1; then
    NICE=(nice -n 15 ionice -c3)
fi
DBT=("${NICE[@]}" ../.venv/bin/dbt)

# extract_days is a dbt var, not just a shell/Python env var, because the
# incremental models' retention post-hooks prune to this same window --
# one number, not two to keep in sync with extract.py.
DBT_VARS=(--vars "{extract_days: ${EXTRACT_DAYS}}")

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
# range has holes. That is expected and handled
# (int_pings_segmented_to_journeys breaks a journey on any gap over 20 min;
# int_journeys_shaped_and_directed discards journeys whose end falls in a
# hole) -- but it is worth saying out loud how much data there is.
echo "==> $(ls data/bus_positions_*.parquet 2>/dev/null | wc -l) daily parquet files in data/"

# No packages.yml: this project deliberately has no package dependencies, so
# there is nothing for `dbt deps` to do. The guard is here so that adding one
# later does not silently break the build.
if [[ -f packages.yml ]]; then
    echo "==> dbt deps"
    "${DBT[@]}" deps --profiles-dir .
fi

# $STAGED is always a fresh per-run throwaway. $BUILD is now the persistent
# incremental warehouse and is left alone unless FULL_REFRESH=1: dbt's
# incremental models only take their fast path when the target table already
# exists, so deleting this every run would make "incremental" a no-op.
rm -f "$STAGED" "$STAGED.wal"

RUN_FLAGS=("${DBT_VARS[@]}")
if [[ "$FULL_REFRESH" == "1" ]]; then
    echo "==> FULL_REFRESH=1: wiping $BUILD and forcing a from-scratch rebuild"
    rm -f "$BUILD" "$BUILD.wal"
    RUN_FLAGS+=(--full-refresh)
fi

echo "==> dbt run   (into $BUILD, at low priority)"
"${DBT[@]}" run --profiles-dir . "${RUN_FLAGS[@]}"

echo "==> dbt test"
"${DBT[@]}" test --profiles-dir . "${DBT_VARS[@]}"

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

# The contract gate. validate.py runs under ghost-site's own uv environment
# (pyyaml/duckdb are real dependencies there now), not dbt's -- so this
# subshell steps back to the repo root, where pyproject.toml lives, rather
# than fighting dbt's own venv or `uv run`'s cwd handling from inside dbt/.
# By default it checks BOTH contracts (bus-positions against live QuestDB,
# bus-marts against $STAGED) plus the ontology cross-check -- exactly the
# "gate a rebuild" mode its own docstring describes. QuestDB being briefly
# unreachable degrades to a skip, not a failure, so this stays safe to run
# even with SKIP_EXTRACT=1 and no live feed. `set -e` means a real failure
# here stops the script before the mv below -- the previous good marts keep
# serving rather than being replaced with something that broke a contract.
echo "==> validating contracts against the candidate marts"
(cd .. && uv run python3 contracts/validate.py --marts "dbt/$STAGED")

# Atomic within a filesystem: readers never see a partial file. The old .wal
# must go with the old file, or DuckDB will try to replay it against the new one.
mv -f "$STAGED" "$SERVED"
rm -f "$SERVED.wal"

elapsed=$(( $(date +%s) - started ))
echo
echo "==> published $SERVED ($(du -h "$SERVED" | cut -f1)) in $((elapsed / 60))m $((elapsed % 60))s"
echo "    the API can open it read-only; it was never locked during the build."
echo "    $BUILD ($(du -h "$BUILD" | cut -f1)) kept as the incremental warehouse."
