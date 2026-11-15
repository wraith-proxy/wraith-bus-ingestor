#!/usr/bin/env bash
#
# One-off weekend cutover (#43, card B12/B14): switch the dbt build from the
# hand-written project to the contract-first one without losing the 8-day
# history that lives ONLY in the persistent warehouse
# dbt/wraith_bus_ingestor_build.duckdb (data/ holds just EXTRACT_DAYS=4).
#
#   ops/cutover.sh --dry-run     preflight for real, echo every other stage
#   ops/cutover.sh               the cutover (asks for the owner's go before
#                                the one step that reads live data)
#   ops/cutover.sh --no-prompt   as above, but stop (exit 3) instead of asking
#
#   STAGE 0 preflight  not root, no build running, memory and disk enough
#   STAGE 1 backup     whole $BUILD to the backup dir (.part until complete)
#   STAGE 2 copies     BASELINE and CANDIDATE = two copies of $BUILD (history
#                      rides along and is NEVER rebuilt), each with its own
#                      code: OLD_REF for baseline, HEAD for candidate, both
#                      sharing the same data/ (so SKIP_EXTRACT semantics)
#   STAGE 3 build      dbt run in each copy, sequentially, low priority
#   STAGE 4 compare    ops/cutover_compare.py on the three marts and the two history
#                      tables (they ride in the copy): keys and counts EXACT, DOUBLE
#                      columns within tolerance. Then dbt test against the
#                      candidate copy. Any mismatch or failing test aborts here.
#   STAGE 5 validate   datacontract lint (offline) and contracts/validate.py
#                      on the candidate. The validate step is printed and
#                      waits for the owner's "go"; it never runs unasked.
#   STAGE 6 swap       hard-link the old file to $BUILD.pre-cutover-<stamp>,
#                      then one atomic mv of the candidate over $BUILD
# Nothing writes to $BUILD before STAGE 6. Logs: ops/logs/cutover-<stamp>/.
# Rollback and its limit: docs/runbook-cutover.md.
#
# Env overrides (used by ops/test_cutover.py): CUTOVER_DB CUTOVER_DATA
# CUTOVER_WORK CUTOVER_BACKUP_DIR CUTOVER_LOGDIR CUTOVER_UNIT
# CUTOVER_MIN_MEM_MB CUTOVER_TABLES CUTOVER_OLD_REF CUTOVER_BUILD_CMD
# CUTOVER_SRC_ONLY CUTOVER_TEST_CMD CUTOVER_STATE_CMD CUTOVER_LINT_CMD CUTOVER_VALIDATE_CMD
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# Tools: the repo venv on a dev box, else whatever is on PATH (the CI image bakes them and has
# no .venv, and a uv-managed run there would resolve and download the whole project).
# Resolved one tool at a time: the repo venv has python but not always datacontract (the
# 2026-11-15 live run found it in ~/.local/bin only), so each falls back to PATH on its own.
PY=python3; DCLI=datacontract; DBT=dbt
[[ -x "$REPO/.venv/bin/python3" ]] && PY="$REPO/.venv/bin/python3"
[[ -x "$REPO/.venv/bin/datacontract" ]] && DCLI="$REPO/.venv/bin/datacontract"
[[ -x "$REPO/.venv/bin/dbt" ]] && DBT="$REPO/.venv/bin/dbt"
DRY=0; PROMPT=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --no-prompt) PROMPT=0 ;;
    *) echo "usage: $0 [--dry-run] [--no-prompt]" >&2; exit 2 ;;
  esac
done

DB="${CUTOVER_DB:-$REPO/dbt/wraith_bus_ingestor_build.duckdb}"
DATA="${CUTOVER_DATA:-$REPO/dbt/data}"
WORK="${CUTOVER_WORK:-$(dirname "$DB")/cutover-work}"
BACKUP_DIR="${CUTOVER_BACKUP_DIR:-/mnt/vault2/wraith-bus-ingestor-build-backup}"
UNIT="${CUTOVER_UNIT:-wraith-bus-build.service}"
# 13000M is the build unit's MemoryMax (spike c: MetricFlow adds ~0.2G on top).
MIN_MEM_MB="${CUTOVER_MIN_MEM_MB:-13000}"
TABLES="${CUTOVER_TABLES:-eta_models localities line_punctuality int_eta_daily_stats int_journey_delays}"
OLD_REF="${CUTOVER_OLD_REF:-origin/main}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOGDIR="${CUTOVER_LOGDIR:-$REPO/ops/logs}/cutover-$STAMP"
# absolute: the build runs inside the extracted copies, where ../.venv does not exist
DEFAULT_BUILD="$DBT run --profiles-dir . --vars \"{extract_days: 4}\""
# git archive omits the gitignored dbt/dbt_packages, so each copy links to the repo's
PACKAGES="${CUTOVER_PACKAGES:-$REPO/dbt/dbt_packages}"
BUILD_CMD="${CUTOVER_BUILD_CMD:-$DEFAULT_BUILD}"
DEFAULT_TEST="$DBT test --profiles-dir . --vars \"{extract_days: 4}\""
TEST_CMD="${CUTOVER_TEST_CMD:-$DEFAULT_TEST}"

mkdir -p "$LOGDIR"
LOG="$LOGDIR/cutover.log"
exec > >(tee -a "$LOG") 2>&1

say()  { echo "[$(date -u +%H:%M:%S)] $*"; }
stage() { echo; say "== $*"; }
die()  { say "!! $*"; exit "${2:-1}"; }
# in a dry run, mutating steps only announce themselves
do_or_say() { if (( DRY )); then say "   (dry-run) would: $*"; else "$@"; fi; }

say "cutover $STAMP  dry-run=$DRY  db=$DB  log=$LOG"

stage "STAGE 0 preflight"
(( EUID == 0 )) && die "refusing to run as root (a root-owned warehouse fails the nightly build)"
[[ -f "$DB" ]] || die "no warehouse at $DB"
[[ -e "$DB.wal" ]] && die "$DB.wal exists: a build holds or crashed on the file"
# Unit states: system level only (a user-level query prints a second line and masked the
# real state). CUTOVER_STATE_CMD names a stub taking the unit and printing its ActiveState.
unit_state() {
  if [[ -n "${CUTOVER_STATE_CMD:-}" ]]; then "$CUTOVER_STATE_CMD" "$1"
  else systemctl show -p ActiveState --value "$1" 2>/dev/null; fi
}
check_no_build() {
  [[ "$UNIT" == none && -z "${CUTOVER_STATE_CMD:-}" ]] && return 0
  [[ -z "${CUTOVER_STATE_CMD:-}" ]] && ! command -v systemctl >/dev/null && return 0
  local u st
  for u in "$UNIT" wraith-bus-extract.service wraith-bus-extract.path; do
    [[ "$u" == none ]] && continue
    st="$(unit_state "$u" | head -n1)"
    [[ "$st" == active || "$st" == activating ]] && return 1
  done
  return 0
}
check_no_build || die "$UNIT, wraith-bus-extract.service or wraith-bus-extract.path is active: stop the extract timer and path unit and wait for the build to finish"
avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
say "memory available ${avail_mb} MB, need ${MIN_MEM_MB} MB"
(( avail_mb >= MIN_MEM_MB )) || die "not enough memory: ${avail_mb} MB < ${MIN_MEM_MB} MB (stop osrm/backend/tiles first)"
size_kb=$(du -k "$DB" | cut -f1)
mkdir -p "$(dirname "$WORK")" 2>/dev/null
free_work_kb=$(df -Pk "$(dirname "$DB")" | awk 'NR==2 {print $4}')
say "warehouse $((size_kb/1024)) MB; free beside it $((free_work_kb/1024)) MB (need 2 copies + 1 GB)"
(( free_work_kb >= 2*size_kb + 1048576 )) || die "not enough disk beside $DB for two copies"
if [[ -d "$(dirname "$BACKUP_DIR")" ]]; then
  free_bk_kb=$(df -Pk "$(dirname "$BACKUP_DIR")" | awk 'NR==2 {print $4}')
  (( free_bk_kb >= size_kb + 1048576 )) || die "not enough disk for the backup in $BACKUP_DIR"
else
  (( DRY )) || die "backup parent $(dirname "$BACKUP_DIR") does not exist"
fi
if (( ! DRY )) && [[ -z "${CUTOVER_SRC_ONLY:-}" && ! -d "$PACKAGES" ]]; then
  die "no dbt_packages at $PACKAGES (gitignored, not in the git archive): run 'dbt deps' in $REPO/dbt first"
fi
# WORK is rm -rf'd later: it must never be the data dir, hold it, or be a directory that
# is not a previous cutover-work dir (marker file).
case "$WORK/" in "$DATA"/*) die "WORK must not be or sit inside the data dir $DATA" ;; esac
case "$DATA/" in "$WORK"/*) die "WORK must not contain the data dir $DATA" ;; esac
if [[ "$WORK" == "$REPO"/* && "$WORK" != "$REPO/dbt/cutover-work" ]]; then
  die "WORK must not sit inside the repo tree (only the default $REPO/dbt/cutover-work)"
fi
if [[ -e "$WORK" ]] && [[ ! -d "$WORK" || ( -n "$(ls -A "$WORK" 2>/dev/null)" && ! -f "$WORK/.cutover-work" ) ]]; then
  die "WORK $WORK exists and is not a cutover-work dir (no .cutover-work marker): refusing to delete it"
fi
if [[ -z "${CUTOVER_SRC_ONLY:-}" ]]; then
  [[ "$(git -C "$REPO" rev-parse "$OLD_REF^{tree}" 2>/dev/null)" ]] || die "cannot resolve baseline ref $OLD_REF"
  [[ "$(git -C "$REPO" rev-parse "$OLD_REF^{tree}")" != "$(git -C "$REPO" rev-parse 'HEAD^{tree}')" ]] \
    || die "baseline $OLD_REF and HEAD have the same tree: the compare would prove nothing. Set CUTOVER_OLD_REF to the commit or tag from before the merge"
fi
# every tool a stage will really run must resolve now, not hours into the build; a tool whose
# stage is replaced by a CUTOVER_*_CMD override is not demanded
need() {
  if [[ "$1" == */* ]]; then [[ -x "$1" ]] && return 0
  else command -v "$1" >/dev/null && return 0; fi
  die "$2 not found: looked in $REPO/.venv/bin/$2, then PATH=$PATH"
}
[[ -n "${CUTOVER_VALIDATE_CMD:-}" ]] || need "$PY" python3
[[ -n "${CUTOVER_LINT_CMD:-}" ]] || need "$DCLI" datacontract
[[ -n "${CUTOVER_BUILD_CMD:-}" && -n "${CUTOVER_TEST_CMD:-}" ]] || need "$DBT" dbt
say "tools: python=$PY datacontract=$DCLI dbt=$DBT"
say "preflight ok"

stage "STAGE 1 backup"
do_or_say bash -c 'mkdir -p "$1" && cp "$2" "$1/$(basename "$2").'"$STAMP"'.part" \
  && mv "$1/$(basename "$2").'"$STAMP"'.part" "$1/$(basename "$2").'"$STAMP"'" \
  && cmp "$2" "$1/$(basename "$2").'"$STAMP"'"' _ "$BACKUP_DIR" "$DB" \
  || die "backup failed; nothing changed"
(( DRY )) || say "backup: $BACKUP_DIR/$(basename "$DB").$STAMP  (this is the rollback)"

BASE="$WORK/baseline/dbt"; CAND="$WORK/candidate/dbt"
stage "STAGE 2 copies  (baseline = $OLD_REF code, candidate = HEAD code, history copied not rebuilt)"
if (( DRY )); then
  say "   (dry-run) would: copy $DB twice into $WORK, extract code, link data/ -> $DATA"
else
  [[ "$(stat -c %d "$(dirname "$DB")")" == "$(stat -c %d "$(dirname "$WORK")")" ]] \
    || die "WORK must be on the same filesystem as $DB (the swap is a rename)"
  rm -rf "$WORK"; mkdir -p "$BASE" "$CAND"; : > "$WORK/.cutover-work"
  if [[ -z "${CUTOVER_SRC_ONLY:-}" ]]; then
    git -C "$REPO" archive "$OLD_REF" dbt | tar -C "$WORK/baseline" -xf - || die "cannot extract $OLD_REF"
    git -C "$REPO" archive HEAD dbt | tar -C "$WORK/candidate" -xf - || die "cannot extract HEAD"
  fi
  for d in "$BASE" "$CAND"; do
    cp "$DB" "$d/$(basename "$DB")" || die "copy failed"
    ln -sfn "$DATA" "$d/data"
    if [[ -z "${CUTOVER_SRC_ONLY:-}" ]]; then ln -sfn "$PACKAGES" "$d/dbt_packages"; fi
  done
  say "copies ready in $WORK"
fi

stage "STAGE 3 build  (baseline then candidate, nice 10, same data/)"
for role in baseline candidate; do
  d="$BASE"; [[ $role == candidate ]] && d="$CAND"
  if (( DRY )); then
    say "   (dry-run) would: in $d run: $BUILD_CMD"
  else
    say "building $role"
    ( cd "$d" && CUTOVER_ROLE=$role nice -n 10 bash -c "$BUILD_CMD" ) > "$LOGDIR/build-$role.log" 2>&1 \
      || die "$role build failed, see $LOGDIR/build-$role.log; live file untouched"
  fi
done

stage "STAGE 4 compare  (keys and counts exact, doubles within tolerance; then dbt test on the candidate)"
if (( DRY )); then
  say "   (dry-run) would: compare $TABLES"
  say "   (dry-run) would: in the candidate copy $CAND run: $TEST_CMD"
else
  (cd "$REPO" && "$PY" ops/cutover_compare.py "$BASE/$(basename "$DB")" \
      "$CAND/$(basename "$DB")" $TABLES) || die "compare failed; live file untouched"
  say "dbt test on the candidate"
  ( cd "$CAND" && nice -n 10 bash -c "$TEST_CMD" ) > "$LOGDIR/test-candidate.log" 2>&1 \
    || die "dbt test failed on the candidate, see $LOGDIR/test-candidate.log; live file untouched"
fi

stage "STAGE 5 validate  (lint offline; live validate needs the owner's go)"
LINT="${CUTOVER_LINT_CMD:-for f in contracts/*.odcs.yaml; do $DCLI lint \"\$f\" || exit 1; done}"
VAL="${CUTOVER_VALIDATE_CMD:-$PY contracts/validate.py --marts \"$CAND/$(basename "$DB")\" --contract contracts/bus-positions.odcs.yaml --contract contracts/bus-marts.odcs.yaml}"
if (( DRY )); then
  say "   (dry-run) would: lint, then print and wait for go: $VAL"
else
  (cd "$REPO" && bash -c "$LINT") || die "contract lint failed; live file untouched"
  if [[ -z "${CUTOVER_VALIDATE_CMD:-}" ]]; then
    say "owner go needed: validate reads the live QuestDB. Command:"
    say "   $VAL"
    (( PROMPT )) || die "stopping: owner go not given (--no-prompt). Candidate kept in $WORK" 3
    read -r -p "type 'go' to run it: " ans < /dev/tty || ans=""
    [[ "$ans" == go ]] || die "owner go not given; candidate kept in $WORK" 3
  fi
  (cd "$REPO" && bash -c "$VAL") || die "validate failed; live file untouched"
fi

stage "STAGE 6 swap  (old file kept, one atomic mv)"
KEEP="$DB.pre-cutover-$STAMP"
if (( DRY )); then
  say "   (dry-run) would: ln $DB $KEEP ; mv -f candidate $DB ; rm -f $DB.wal"
else
  check_no_build || die "a build or extract started while waiting: not swapping. Candidate kept in $WORK" 4
  ln "$DB" "$KEEP" || die "cannot keep the old file"
  mv -f "$CAND/$(basename "$DB")" "$DB" || die "swap failed (old file still in place)"
  rm -f "$DB.wal"
  say "swapped. old file kept as $KEEP, backup in $BACKUP_DIR"
  say "rollback: docs/runbook-cutover.md (lossless only within EXTRACT_DAYS=4)"
  say "baseline copy $WORK can be deleted once three nightlies pass"
fi
say "done"
