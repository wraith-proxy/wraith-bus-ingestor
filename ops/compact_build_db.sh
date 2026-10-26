#!/usr/bin/env bash
#
# One-off (ADR-0010): shrink dbt's scratch warehouse by copying it into a
# fresh file.
#
#   ops/compact_build_db.sh            back up, build, verify, swap (~10 min)
#   ops/compact_build_db.sh --check    preflight only: writes nothing
#
# Why a copy and not a DELETE: DuckDB never shrinks a file. The segmentation
# post-hook's deletes, and every nightly's delete+insert, leave their blocks
# inside dbt/wraith_bus_ingestor_build.duckdb for reuse, so it stays at its
# high-water mark (9.9G on 2026-10-25, almost all of it
# int_pings_segmented_to_journeys) however little of it is still live. A file
# written with CREATE TABLE AS holds only what is copied into it -- the same
# reason build.sh publishes the marts into a fresh file, not a pruned copy.
#
# What is copied: only the three incremental tables, the ones dbt cannot
# rebuild from scratch in one night:
#   int_pings_segmented_to_journeys   only the dates its own post-hook keeps at
#                                     extract_days=4 (max - 4 days, 5 dates),
#                                     and without shoelace_term (the retired
#                                     direction heuristic's column, ~8% of it)
#   int_eta_daily_stats               whole (the history, history_days=8)
#   int_journey_delays                whole (the history, history_days=8)
# Everything else (table models, views, marts) is not copied: dbt rebuilds the
# tables and recreates the views on its next run, before anything reads them.
#
#   STAGE 0  preflight   refuses 01:30-05:00 UTC (precleanup 02:00, nightly
#                        03:00), a build in progress (the unit active or
#                        activating, or dbt/build.sh/extract.py running), any
#                        process holding the file, a leftover .wal, root, too
#                        little space on / or vault2, or either history table
#                        missing or under 9 distinct stats_date (a thin history
#                        copied is a thin history kept)
#   STAGE 1  backup      the whole file to /mnt/archive/build_db_backup/,
#                        throttled, as .part until its size matches. This copy
#                        is the rollback.
#   STAGE 2  build       a fresh file beside the old one (same disk, so the
#                        swap is a rename) with the old one attached READ_ONLY,
#                        then both reopened read-only and counted table by table
#   STAGE 3  swap        the preflight checks again, then mv over the old file
# Nothing writes to the real warehouse before STAGE 3. Any failure before it
# deletes the half-built file and leaves the old one exactly as it was.
#
# --dry-run does STAGE 0 and 2 (the build, counted, then deleted) and writes no
# backup and no swap.
#
# Everything goes to ops/logs/compact-<UTC stamp>/compact.log, with a live tail
# in your terminal. Run it as your own user, never root: a root-owned warehouse
# fails the 03:00 build (it runs as you). Exit status 0 means swapped and
# verified; the last lines print the rollback, which is the backup
# AND the pre-PR2 code (the restored file still has shoelace_term and 9 dates,
# which PR2's models fail on), or FULL_REFRESH=1 under a replay-free build.
#
# COMPACT_DB, COMPACT_ARCHIVE, COMPACT_BACKUP_DIR, COMPACT_PY, COMPACT_LOGDIR
# and COMPACT_UTC_HHMM exist for ops/test_compact_build_db.py, which runs this against a throwaway
# warehouse. COMPACT_TEST_FAIL=1 deletes one copied row before the count, to
# prove the failure path.
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${COMPACT_DB:-$REPO/dbt/wraith_bus_ingestor_build.duckdb}"
NEW="$DB.compact"
ARCHIVE="${COMPACT_ARCHIVE:-/mnt/archive/wraith-bus-ingestor-archive}"   # its marker proves the mount
BACKUP_DIR="${COMPACT_BACKUP_DIR:-/mnt/archive/build_db_backup}"
# The environment build.sh publishes the marts with (../.venv, duckdb per uv.lock).
PY="${COMPACT_PY:-$REPO/.venv/bin/python}"
UNIT="wraith-bus-build.service"
# The segmentation post-hook deletes ping_local_date < max - retention_days()
# days; at extract_days=4 this keeps exactly the complement (5 dates).
KEEP_DAYS=4

CHECK=0; DRY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "usage: $0 [--check | --dry-run]"; exit 2 ;;
  esac
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOGDIR="${COMPACT_LOGDIR:-$REPO/ops/logs/compact-$STAMP}"
MAIN_LOG="$LOGDIR/compact.log"
BACKUP="$BACKUP_DIR/$(basename "$DB" .duckdb)-$STAMP.duckdb"
TAILPID=""; SWAPPED=0; SIZE=0; MTIME=0

# Low priority, as build.sh: the API shares the disk and the cores.
NICE=(nice -n 15)
if command -v ionice >/dev/null 2>&1 && ionice -c3 true >/dev/null 2>&1; then
  NICE=(nice -n 15 ionice -c3)
fi

# ---- output helpers ---------------------------------------------------------
ts() { date -u +%H:%M:%S; }
major() { echo; echo "================================================================="; echo "[$(ts)] ### $*"; echo "================================================================="; }
minor() { echo "[$(ts)]   -> $*"; }
good()  { echo "[$(ts)]   OK   $*"; }
bad()   { echo "[$(ts)]   FAIL $*"; }
die()   { bad "$*"; exit 1; }
bytes() { stat -c %s "$1" 2>/dev/null || echo 0; }
gb()    { awk -v b="${1:-0}" 'BEGIN{printf "%.1fG", b/1073741824}'; }
avail() { df --output=avail -B1 "$1" | tail -1 | tr -dc 0-9; }

# ---- log + live tail --------------------------------------------------------
setup_log() {
  mkdir -p "$LOGDIR"; : > "$MAIN_LOG"
  if [[ -t 1 ]]; then                # a live view only when there is a terminal for it
    tail -n +1 -f "$MAIN_LOG" &
    TAILPID=$!
  fi
  exec >>"$MAIN_LOG" 2>&1            # from here on, stdout AND stderr go to the log
}

# ---- never leave a half-built file ------------------------------------------
cleanup() {
  local rc=$?
  trap - EXIT
  if [[ $SWAPPED -eq 0 ]]; then
    rm -rf "$NEW" "$NEW.wal" "$NEW.tmp" "$BACKUP.part"
    [[ $rc -ne 0 ]] && echo "[$(ts)] RESULT: FAIL (exit $rc) -- $DB untouched"
  fi
  if [[ -n "$TAILPID" ]]; then sleep 1; kill "$TAILPID" 2>/dev/null; fi
  exit "$rc"
}

# What must still hold at the moment of the swap, not just at the start.
guard() {
  local hhmm=$((10#${COMPACT_UTC_HHMM:-$(date -u +%H%M)}))
  (( hhmm < 130 || hhmm >= 500 )) || die "it is $(printf %04d "$hhmm") UTC: refusing inside 01:30-05:00 (precleanup 02:00, nightly build 03:00)"
  # Not `systemctl is-active`: a Type=oneshot unit is 'activating', not
  # 'active', for the whole of its run, and is-active says no to that.
  local state; state=$(systemctl show -p ActiveState --value "$UNIT" 2>/dev/null)
  [[ "$state" != activ* && "$state" != reloading ]] || die "$UNIT is $state: a build is running"
  local pat="${COMPACT_PGREP_PATTERN:-dbt (run|build|test)|build\.sh|extract\.py}"   # override only in tests
  pgrep -f "$pat" >/dev/null 2>&1 && die "dbt, build.sh or extract.py is running: $(pgrep -af "$pat" | head -3)"
  # Without root fuser sees only your own processes -- dbt, build.sh and
  # incident.py all run as you. Any other writer still holds DuckDB's lock,
  # which makes STAGE 2's READ_ONLY attach fail.
  if fuser -s "$DB" 2>/dev/null; then die "a process has the warehouse open: $(fuser "$DB" 2>&1)"; fi
  [[ ! -e "$DB.wal" ]] || die "$DB.wal exists: not closed cleanly, and a copy of the .duckdb alone would miss it"
}

# ---- stage 0: preflight -----------------------------------------------------
preflight() {
  major "STAGE 0  Preflight"
  [[ $EUID -ne 0 ]] || die "run as your own user, not root: a root-owned warehouse fails the 03:00 build"
  [[ -f "$DB" ]] || die "$DB not found"
  guard
  good "outside 01:30-05:00 UTC, $UNIT not running, no dbt/build.sh/extract.py, nothing has the file open, no .wal"
  [[ -x "$PY" ]] && good "python: $PY" || die "$PY not found (uv sync at the repo root)"
  [[ -f "$ARCHIVE/.this-is-vault2" ]] && good "vault2 mounted ($ARCHIVE)" || die "$ARCHIVE/.this-is-vault2 missing: vault2 is not mounted, refusing to back up onto the wrong disk"
  SIZE=$(bytes "$DB"); MTIME=$(stat -c %Y "$DB")
  minor "warehouse: $DB, $(gb "$SIZE")"
  # The new file holds less than the old one, so the old size bounds both disks.
  local ssd vault; ssd=$(avail "$(dirname "$DB")"); vault=$(avail "$ARCHIVE")
  (( ssd > SIZE )) && good "$(gb "$ssd") free on the warehouse's disk" || die "only $(gb "$ssd") free on the warehouse's disk, need $(gb "$SIZE")"
  (( vault > SIZE )) && good "$(gb "$vault") free on vault2" || die "only $(gb "$vault") free on vault2, need $(gb "$SIZE")"
  # The history tables are the one thing that cannot be rebuilt from the
  # parquet in a night; refuse to compact (and later seed) a thin one.
  local h; h=$("$PY" - "$DB" <<'PYEOF'
import sys

import duckdb

con = duckdb.connect(sys.argv[1], read_only=True)
bad = []
for t in ("int_eta_daily_stats", "int_journey_delays"):
    try:
        n = con.execute(f"select count(distinct stats_date) from {t}").fetchone()[0]
    except duckdb.Error:
        n = -1
    print(f"{t}={n}")
    if n < 9:
        bad.append(t)
sys.exit(1 if bad else 0)
PYEOF
  ) && good "history tables hold >= 9 stats_date ($(echo $h))" || die "history too thin or missing (need >= 9 distinct stats_date in both): $(echo $h)"
}

# ---- stage 1: backup --------------------------------------------------------
backup() {
  major "STAGE 1  Back up to vault2 (the rollback)"
  mkdir -p "$BACKUP_DIR" || die "cannot create $BACKUP_DIR"
  minor "rsync --bwlimit=100000 (~100MB/s) -> $BACKUP"
  "${NICE[@]}" rsync --bwlimit=100000 --times "$DB" "$BACKUP.part" || die "rsync failed"
  # rsync checksums every file it writes; the size check catches a short copy.
  [[ $(bytes "$BACKUP.part") -eq $SIZE ]] || die "backup is $(bytes "$BACKUP.part") bytes, the warehouse $SIZE"
  mv "$BACKUP.part" "$BACKUP" || die "cannot rename $BACKUP.part"
  good "backed up, $SIZE bytes, sizes match"
}

# ---- stage 2: build + verify ------------------------------------------------
build() {
  major "STAGE 2  Build the compact copy, then count it table by table"
  rm -rf "$NEW" "$NEW.wal" "$NEW.tmp"   # left by an interrupted run
  "${NICE[@]}" "$PY" - "$DB" "$NEW" "$KEEP_DAYS" <<'PYEOF' || die "build or verification failed"
import os
import sys

import duckdb

old, new, keep_days = sys.argv[1], sys.argv[2], int(sys.argv[3])
SEG = 'int_pings_segmented_to_journeys'
HISTORY = ('int_eta_daily_stats', 'int_journey_delays')
COPIED = (SEG,) + HISTORY


def source(con, schema, table):
    """The SELECT that fills one table. For segmentation: the rows its post-hook
    keeps (the complement of its DELETE) minus the dead column, so the next
    nightly finds exactly what it would have left itself. The history tables
    are copied whole."""
    src = f'old."{schema}"."{table}"'
    if table != SEG:
        return f'select * from {src}'
    cols = [c for (c,) in con.execute(
        "select column_name from duckdb_columns() "
        "where database_name = 'old' and schema_name = ? and table_name = ?",
        [schema, table]).fetchall()]
    drop = ' exclude (shoelace_term)' if 'shoelace_term' in cols else ''
    return (f'select *{drop} from {src} where ping_local_date >= '
            f"(select max(ping_local_date) from {src}) - interval '{keep_days} days'")


con = duckdb.connect(new)
con.execute("set memory_limit = '4GB'")
con.execute("set preserve_insertion_order = false")   # as profiles.yml
con.execute(f"attach '{old}' as old (read_only)")
found = con.execute(
    "select schema_name, table_name from duckdb_tables() "
    "where database_name = 'old' order by all").fetchall()
tables = [t for t in found if t[1] in COPIED]
if len(tables) != len(COPIED):
    print(f"  FAIL the old file lacks some of {', '.join(COPIED)}")
    sys.exit(1)
print(f"  not copied (dbt rebuilds or recreates them): "
      f"{', '.join(t for _, t in found if t not in COPIED) or 'none'}")
for schema, table in tables:
    con.execute(f'create schema if not exists "{schema}"')
    con.execute(f'create table "{schema}"."{table}" as {source(con, schema, table)}')
if os.environ.get('COMPACT_TEST_FAIL') == '1':
    schema, table = tables[-1]
    con.execute(f'delete from "{schema}"."{table}" where rowid = (select min(rowid) from "{schema}"."{table}")')
con.execute('checkpoint')
con.close()

# Counted from a fresh read-only open: what is checked is what reached the
# disk, not what the writing connection believed it wrote.
con = duckdb.connect(new, read_only=True)
con.execute(f"attach '{old}' as old (read_only)")
failed = 0
for schema, table in tables:
    want = con.execute(f'select count(*) from ({source(con, schema, table)})').fetchone()[0]
    got = con.execute(f'select count(*) from "{schema}"."{table}"').fetchone()[0]
    failed += want != got
    print(f"  {'OK  ' if want == got else 'FAIL'} {table:<34} {got:>13,} rows (expected {want:,})")
    if table in HISTORY:
        want = con.execute(f'select count(distinct stats_date) from old."{schema}"."{table}"').fetchone()[0]
        got = con.execute(f'select count(distinct stats_date) from "{schema}"."{table}"').fetchone()[0]
        failed += want != got
        print(f"  {'OK  ' if want == got else 'FAIL'} {table:<34} {got:>13,} stats_date (expected {want:,})")
copied = con.execute(
    "select count(*) from duckdb_tables() where database_name = current_database()").fetchone()[0]
if copied != len(tables):
    print(f"  FAIL the new file has {copied} tables, expected {len(tables)}")
    failed += 1
con.close()
sys.exit(1 if failed else 0)
PYEOF
  good "every table's row count matches; $(gb "$(bytes "$NEW")") (was $(gb "$SIZE"))"
}

# ---- stage 3: swap ----------------------------------------------------------
swap() {
  major "STAGE 3  Swap"
  guard   # a build, or anything else, may have started since STAGE 0
  [[ $(bytes "$DB") -eq $SIZE && $(stat -c %Y "$DB") -eq $MTIME ]] || die "the warehouse changed during the run: the backup no longer matches it"
  [[ $(bytes "$BACKUP") -eq $SIZE ]] || die "$BACKUP missing or a different size: no rollback, not swapping"
  # One rename on one disk: atomic, and it is what frees the old file.
  mv -f "$NEW" "$DB" || die "mv failed"
  SWAPPED=1
  good "swapped in: $DB, $(gb "$(bytes "$DB")")"
}

main() {
  if [[ $CHECK -eq 1 ]]; then
    preflight; echo; echo "  --check: preflight only, nothing written. Run for real with:  $0"; exit 0
  fi
  setup_log
  trap cleanup EXIT
  trap 'exit 130' INT TERM
  echo "compact_build_db.sh  $(date -u '+%F %T UTC')   log: $MAIN_LOG"
  preflight
  if [[ $DRY -eq 1 ]]; then
    build
    major "RESULT: PASS (dry run)  would be $(gb "$(bytes "$NEW")") from $(gb "$SIZE"); nothing backed up or swapped"
    rm -f "$NEW" "$NEW.wal"
    return
  fi
  backup
  build
  swap
  major "RESULT: PASS  $(gb "$SIZE") -> $(gb "$(bytes "$DB")")"
  minor "table models and views come back with the next dbt run; nothing reads the warehouse before it"
  minor "ROLLBACK needs BOTH:  rsync --bwlimit=100000 $BACKUP $DB"
  minor "  and the pre-PR2 code checked out (the backup still has shoelace_term and 9 dates, which PR2's models fail on),"
  minor "  or FULL_REFRESH=1 under a replay-free build"
  minor "delete the backup once a nightly has passed on the new file"
  minor "log: $MAIN_LOG"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main; fi

