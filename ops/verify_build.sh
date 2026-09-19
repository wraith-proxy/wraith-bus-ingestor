#!/usr/bin/env bash
#
# Prove the nightly build end to end, on demand, before pushing anything.
#
#   sudo ops/verify_build.sh              # both runs below, roughly 2.5-3 hours
#   ops/verify_build.sh --check           # preflight only: no sudo, stops nothing
#   ops/verify_build.sh --dry-run         # the whole flow with fake builds (seconds)
#
# Why: the 2026-09-18/19 OOM-kills, and ADR-0005's dbt v2 detour, showed that a
# clean isolated `dbt run` proves little -- the failures only appeared under the
# real nightly conditions. So this runs both, in order, and measures memory.
#
#   Run 1  SKIP_EXTRACT=1 ./build.sh, as YOUR user (never root: it would leave
#          root-owned .duckdb files in dbt/), under `time -v`. Reuses the parquet
#          already in dbt/data/. Peak RSS is the largest single process, i.e. the
#          DuckDB one that OOM'd at profiles.yml's 4GB memory_limit on 2026-09-19.
#   Run 2  the real thing: `systemctl start wraith-bus-build.service` -- extract
#          from QuestDB, build, test, publish, under the unit's MemoryMax cap, with
#          the unit's own container stop/start and nginx reload. Peak memory is
#          sampled from the unit's cgroup and read from the journal.
#
# Everything -- this script's progress, both builds, stderr included -- goes to
# ONE log, and a live `tail -f` of it runs in your terminal. Files:
#   ops/logs/verify-<UTC stamp>/verify.log   everything
#   ops/logs/verify-<UTC stamp>/run1.log     Run 1 alone
# A trap puts every container this script stopped back, however it exits.
# Nothing is pushed, committed or deleted.
#
# Root is used only for what needs it (stopping containers, swap reclaim,
# systemctl). Exit status 0 means both runs passed and the public site is healthy.
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DBT="$REPO/dbt"
UNIT="wraith-bus-build.service"
UNIT_SRC="$REPO/ops/systemd/$UNIT"
UNIT_LIVE="/etc/systemd/system/$UNIT"
MARTS="$DBT/wraith_bus_ingestor.duckdb"
QDB="http://localhost:9001"
RUN_USER="${SUDO_USER:-$(stat -c %U "$REPO")}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
# Exactly what systemd gives the unit's build.sh: no ~/.local/bin on PATH. Run 1 uses it too, so
# a missing-uv style bug (found 2026-09-19) shows in Run 1 instead of an hour into Run 2.
SYSTEMD_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
BUILD_STOP=(ghost-osrm ghost-backend ghost-tiles ghost-grafana)
OPTIONAL_STOP=(ollama immich_machine_learning immich_server immich_postgres immich_redis)
MEMORY_LIMIT_KB=$((4 * 1024 * 1024))   # profiles.yml's DuckDB memory_limit, for the summary

CHECK=0; DRY=0
case "${1:-}" in
  --check) CHECK=1 ;;
  --dry-run) DRY=1 ;;
  "") ;;
  *) echo "usage: $0 [--check | --dry-run]"; exit 2 ;;
esac

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOGDIR="$REPO/ops/logs/verify-$STAMP"
[[ $DRY -eq 1 ]] && LOGDIR="$REPO/ops/logs/dryrun-$STAMP"
MAIN_LOG="$LOGDIR/verify.log"
RUN1_LOG="$LOGDIR/run1.log"
STOPPED=()
TAILPID=""; SAMPLER_PID=""; SCRIPT_START=$(date +%s)
RUN1_RC=""; RUN2_RC=""; PROBLEMS=()

# ---- output helpers ---------------------------------------------------------
ts() { date -u +%H:%M:%S; }
elapsed() { local s=$(( $(date +%s) - $1 )); printf '%dh%02dm%02ds' $((s/3600)) $((s%3600/60)) $((s%60)); }
major() { echo; echo "================================================================="; echo "[$(ts)] ### $*"; echo "================================================================="; }
minor() { echo "[$(ts)]   -> $*"; }
good()  { echo "[$(ts)]   OK   $*"; }
warn()  { echo "[$(ts)]   WARN $*"; }
bad()   { echo "[$(ts)]   FAIL $*"; PROBLEMS+=("$*"); }
die()   { bad "$*"; exit 1; }
# side effects are printed, not run, in --dry-run
act() { if [[ $DRY -eq 1 ]]; then echo "[$(ts)]   [dry-run] would run: $*"; else "$@"; fi; }
kb_to_gb() { awk -v k="${1:-0}" 'BEGIN{printf "%.2f", k/1048576}'; }

# ---- log + live tail --------------------------------------------------------
setup_log() {
  mkdir -p "$LOGDIR"; : > "$MAIN_LOG"
  tail -n +1 -f "$MAIN_LOG" &        # what you watch in the terminal
  TAILPID=$!
  exec >>"$MAIN_LOG" 2>&1            # from here on, stdout AND stderr go to the log
}

# ---- always put things back -------------------------------------------------
cleanup() {
  local rc=$?
  trap - EXIT
  [[ -n "$SAMPLER_PID" ]] && kill "$SAMPLER_PID" 2>/dev/null
  if [[ $DRY -eq 0 && $CHECK -eq 0 ]] && systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    warn "$UNIT is still running -- leaving services alone. Abort it with: sudo systemctl stop $UNIT"
  elif ((${#STOPPED[@]})); then
    major "Restoring services this script stopped"
    for c in "${STOPPED[@]}"; do act docker start "$c" >/dev/null && good "started $c"; done
    act docker exec ghost-web nginx -s reload >/dev/null 2>&1 && good "nginx reloaded (containers get new IPs)"
  fi
  if [[ -n "$TAILPID" ]]; then sleep 1; kill "$TAILPID" 2>/dev/null; fi
  [[ -d "$LOGDIR" && $EUID -eq 0 ]] && chown -R "$RUN_USER":"$RUN_USER" "$LOGDIR" 2>/dev/null
  exit "$rc"
}

# ---- QuestDB / API probes ---------------------------------------------------
qdb() { curl -sG --max-time 20 "$QDB/exec" --data-urlencode "query=$1"; }
latest_build_row() {   # -> "result|tests_passed|tests_failed|tests_warned|timestamp"
  qdb "select result, tests_passed, tests_failed, tests_warned, timestamp from dbt_build_runs order by timestamp desc limit 1" |
    python3 -c "import json,sys; d=json.load(sys.stdin)['dataset']; print('|'.join(str(x) for x in d[0]) if d else 'none')" 2>/dev/null
}
marts_state() {        # -> "marts_state duckdb" from the public API
  curl -s --max-time 15 https://slaycoach.quest/api/model-info |
    python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('marts_state'), d.get('duckdb'))" 2>/dev/null || echo "unreachable -"
}
mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }

# ---- stage 0: preflight -----------------------------------------------------
preflight() {
  major "STAGE 0  Preflight"
  [[ $DRY -eq 1 || $CHECK -eq 1 || $EUID -eq 0 ]] || die "run with sudo:  sudo $0"
  [[ "$RUN_USER" != "root" ]] || die "cannot work out the non-root user to run the build as (set SUDO_USER)"
  minor "repo: $REPO   build runs as: $RUN_USER   log: $MAIN_LOG"
  [[ -x "$DBT/build.sh" ]] && good "dbt/build.sh present" || die "dbt/build.sh missing or not executable"
  [[ -x "$RUN_HOME/.local/bin/uv" ]] && good "uv found at $RUN_HOME/.local/bin/uv (build.sh's contract gate needs it)" || die "uv not found at $RUN_HOME/.local/bin/uv"
  docker ps >/dev/null 2>&1 && good "docker reachable" || die "docker not reachable"
  curl -sf --max-time 10 "$QDB/exec?query=select%201" >/dev/null && good "QuestDB answering" || die "QuestDB not answering on $QDB"
  local days; days=$(ls "$DBT"/data/bus_positions_*.parquet 2>/dev/null | sed -E 's/.*_([0-9-]{10}).*/\1/' | sort -u | wc -l)
  [[ $days -ge 7 ]] && good "dbt/data holds $days days of parquet (Run 1 reuses it)" || die "dbt/data has only $days days of parquet -- Run 1 needs the extract window"
  local free_gb; free_gb=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
  [[ $free_gb -ge 20 ]] && good "${free_gb}G free on the SSD" || die "only ${free_gb}G free on the SSD (need 20G)"
  if diff -q "$UNIT_LIVE" "$UNIT_SRC" >/dev/null 2>&1; then good "$UNIT matches the repo (MemoryMax $(grep -m1 '^MemoryMax' "$UNIT_SRC" | cut -d= -f2))"
  else die "$UNIT_LIVE differs from $UNIT_SRC -- copy it and daemon-reload first, or Run 2 tests the wrong cap"; fi
  # a real dbt process, whoever started it (the [b] keeps this from matching its own command line)
  if systemctl is-active --quiet "$UNIT" 2>/dev/null || pgrep -f "[b]in/dbt (run|test|deps|build)" >/dev/null; then die "a build is already running"; fi
  good "no build already running"
  minor "memory now: $(free -h | awk 'NR==2{print $7" available of "$2}')   swap used: $(free -h | awk 'NR==3{print $3}')"
  BASE_MARTS_MTIME=$(mtime "$MARTS"); BASE_BUILD_ROW=$(latest_build_row); BASE_API=$(marts_state)
  minor "served marts last published: $(date -d @"$BASE_MARTS_MTIME" '+%F %H:%M' 2>/dev/null)"
  minor "public API marts_state / duckdb BEFORE: $BASE_API   (expect 'unreadable False': the alpha-written file, see ADR-0005)"
  minor "latest recorded build: $BASE_BUILD_ROW"
}

# ---- stage 1/2: free memory -------------------------------------------------
stop_containers() {   # stop each named container that is running, remembering it
  for c in "$@"; do
    if docker ps --format '{{.Names}}' | grep -qx "$c"; then
      act docker stop "$c" >/dev/null && { STOPPED+=("$c"); good "stopped $c"; }
    else minor "$c not running, leaving it"; fi
  done
}
free_memory() {
  major "STAGE 1  Free memory for the build"
  minor "build-window consumers (the unit does this too, so Run 1 gets the same headroom):"
  stop_containers "${BUILD_STOP[@]}"
  minor "optional, only you use them, ~4GB (restored at the end):"
  stop_containers "${OPTIONAL_STOP[@]}"
  minor "available memory now: $(free -h | awk 'NR==2{print $7}')"
}
reclaim_swap() {
  major "STAGE 2  Reclaim swap (ops/precleanup.sh, safety-checked, soft-fail)"
  minor "swap used before: $(free -h | awk 'NR==3{print $3}')"
  act "$REPO/ops/precleanup.sh"
  minor "swap used after:  $(free -h | awk 'NR==3{print $3}')"
}

# ---- Run 1 ------------------------------------------------------------------
run1() {
  major "STAGE 3  RUN 1 of 2 -- SKIP_EXTRACT=1 build, as $RUN_USER  (~1h)"
  minor "DuckDB's memory_limit is $(grep -m1 'memory_limit:' "$DBT/profiles.yml" | tr -d ' ')  <- the limit that failed on 2026-09-19"
  local t0; t0=$(date +%s); RUN1_T0=$t0
  if [[ $DRY -eq 1 ]]; then
    if [[ "${VERIFY_DRYRUN_FAIL:-}" == "1" ]]; then   # test the failure path: VERIFY_DRYRUN_FAIL=1 ops/verify_build.sh --dry-run
      { sleep 2; echo "==> dbt run (fake)"; echo "Runtime Error in model int_pings_matched_to_stops"
        echo "  Out of Memory Error: could not allocate block of size 256.0 KiB (3.7 GiB/3.7 GiB used)"
        echo "	Command exited with non-zero status 1"; echo "	Elapsed (wall clock) time (h:mm:ss or m:ss): 0:02.00"
        echo "	Maximum resident set size (kbytes): 4700000"; } 2>&1 | tee "$RUN1_LOG"
      RUN1_RC=1
    else
      { sleep 2; echo "==> dbt run (fake)"; echo "Done. PASS=97 WARN=0 ERROR=0 SKIP=0 TOTAL=97"; echo "Done. PASS=884 WARN=0 ERROR=0 SKIP=0 TOTAL=884"
        echo "	Elapsed (wall clock) time (h:mm:ss or m:ss): 0:02.00"; echo "	Maximum resident set size (kbytes): 4321000"; } 2>&1 | tee "$RUN1_LOG"
      RUN1_RC=0
    fi
  else
    # `env -i`: NOT root's environment (HOME=/root would break uv's cache), and the unit's PATH.
    runuser -u "$RUN_USER" -- env -i HOME="$RUN_HOME" USER="$RUN_USER" LOGNAME="$RUN_USER" PATH="$SYSTEMD_PATH" \
      bash -c 'cd "$1" && exec /usr/bin/time -v env SKIP_EXTRACT=1 ./build.sh' _ "$DBT" 2>&1 | tee "$RUN1_LOG"
    RUN1_RC=${PIPESTATUS[0]}
  fi
  RUN1_PEAK_KB=$(grep -m1 "Maximum resident set size" "$RUN1_LOG" | awk '{print $NF}')
  RUN1_WALL=$(grep -m1 "Elapsed (wall clock)" "$RUN1_LOG" | sed 's/.*: //')
  RUN1_OOM=$(grep -c "Out of Memory Error" "$RUN1_LOG")
  RUN1_PASS=$(grep -oE "Done\. PASS=[0-9]+ WARN=[0-9]+ ERROR=[0-9]+" "$RUN1_LOG" | tail -1)
  minor "Run 1 finished in $(elapsed "$t0"), exit code $RUN1_RC, peak RSS $(kb_to_gb "${RUN1_PEAK_KB:-0}")GB, OOM errors: $RUN1_OOM"
  local pub=0; [[ $DRY -eq 1 || $(mtime "$MARTS") -gt $BASE_MARTS_MTIME ]] && pub=1
  if [[ $RUN1_RC -ne 0 ]]; then bad "Run 1 exited $RUN1_RC (DuckDB out-of-memory errors: $RUN1_OOM)"; minor "last lines of Run 1:"; tail -n 12 "$RUN1_LOG" | sed 's/^/        /'; return 1; fi
  [[ $RUN1_OOM -eq 0 ]] && good "no DuckDB out-of-memory errors" || bad "DuckDB reported $RUN1_OOM out-of-memory error(s)"
  [[ $pub -eq 1 ]] && good "marts published (served file replaced)" || bad "exit 0 but the served marts file was not replaced"
  return 0
}

# ---- Run 2 ------------------------------------------------------------------
sample_rss() {   # every 5s: largest single process + total RSS in the unit's cgroup, in kB -> $1
  local cg pid rss sum one=0 tot=0
  while true; do
    cg=$(systemctl show -p ControlGroup --value "$UNIT" 2>/dev/null)
    if [[ -n "$cg" && -r "/sys/fs/cgroup${cg}/cgroup.procs" ]]; then
      sum=0
      while read -r pid; do
        rss=$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null); rss=${rss:-0}
        sum=$((sum + rss)); (( rss > one )) && one=$rss
      done < "/sys/fs/cgroup${cg}/cgroup.procs"
      (( sum > tot )) && tot=$sum
      echo "$one $tot" > "$1"
    fi
    sleep 5
  done
}
run2() {
  major "STAGE 4  RUN 2 of 2 -- full production run via systemd  (~1.5h)"
  minor "extracts from QuestDB (it restarts between days), builds, tests, publishes, under the unit's cap"
  local t0 since; t0=$(date +%s); since=$(date '+%Y-%m-%d %H:%M:%S'); RUN2_T0=$t0
  local before_mtime; before_mtime=$(mtime "$MARTS")
  : > "$LOGDIR/rss2.txt"
  if [[ $DRY -eq 1 ]]; then
    sleep 2; echo "[dry-run] build.log lines would stream here"; echo "4321000 5432000" > "$LOGDIR/rss2.txt"; RUN2_RC=0
    RUN2_JOURNAL="wraith-bus-build.service: Consumed 10min 2.000s CPU time, 5.6G memory peak, 0B memory swap peak."
  else
    sample_rss "$LOGDIR/rss2.txt" & SAMPLER_PID=$!
    systemctl start "$UNIT" & local sp=$!
    tail -n0 -f "$DBT/build.log" --pid="$sp" >>"$MAIN_LOG" 2>&1 &     # the unit logs to dbt/build.log
    wait "$sp"; RUN2_RC=$?
    sleep 2; kill "$SAMPLER_PID" 2>/dev/null; SAMPLER_PID=""
    RUN2_JOURNAL=$(journalctl -u "$UNIT" --since "$since" --no-pager -o cat 2>/dev/null | grep -E "memory peak" | tail -1)
  fi
  read -r RUN2_ONE_KB RUN2_TOT_KB < "$LOGDIR/rss2.txt" || true
  RUN2_CG_PEAK=$(grep -oE "[0-9.]+[KMGT]? memory peak" <<<"$RUN2_JOURNAL" | head -1)
  minor "Run 2 finished in $(elapsed "$t0"), exit code $RUN2_RC"
  minor "peak RSS, largest single process: $(kb_to_gb "${RUN2_ONE_KB:-0}")GB   all processes together: $(kb_to_gb "${RUN2_TOT_KB:-0}")GB   cgroup peak (includes page cache): ${RUN2_CG_PEAK:-n/a}"
  if [[ $RUN2_RC -ne 0 ]]; then
    bad "$UNIT failed (exit $RUN2_RC): $(systemctl show "$UNIT" -p Result --value 2>/dev/null)"
    journalctl -u "$UNIT" --since "$since" --no-pager -o cat 2>/dev/null | tail -n 8 | sed 's/^/        /'; return 1
  fi
  [[ $DRY -eq 1 || $(mtime "$MARTS") -gt $before_mtime ]] && good "marts republished by the unit" || bad "unit succeeded but the served marts file did not change"
  local row; row=$([[ $DRY -eq 1 ]] && echo "success|884|0|0|now" || latest_build_row)
  minor "latest recorded build: $row   (result|passed|failed|warned|when)"
  IFS='|' read -r r_res r_pass r_fail r_warn _ <<<"$row"
  [[ "$r_res" == "success" && "${r_fail:-1}" == "0" ]] && good "build recorded as success, $r_pass tests passed, $r_warn warned" || bad "latest build row is not a clean success: $row"
  return 0
}

# ---- stage 5: restore + health ----------------------------------------------
health() {
  major "STAGE 5  Restore services and check the public site"
  for c in "${BUILD_STOP[@]}" "${OPTIONAL_STOP[@]}"; do
    docker ps --format '{{.Names}}' | grep -qx "$c" || { act docker start "$c" >/dev/null; minor "started $c"; }
  done
  STOPPED=()
  act docker exec ghost-web nginx -s reload >/dev/null 2>&1; minor "nginx reloaded"
  minor "waiting 45s for containers and the API's marts poll (it re-checks once a minute)"; [[ $DRY -eq 1 ]] || sleep 45
  local u code
  for u in https://www.thomaspeoples.com/grafana/api/health https://slaycoach.quest/tiles/great-britain/0/0/0 https://slaycoach.quest/bus-map/ https://slaycoach.quest/api/buses; do
    code=$([[ $DRY -eq 1 ]] && echo 200 || curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$u")
    [[ "$code" == "200" ]] && good "$code $u" || bad "$code $u"
  done
  local st; st=$([[ $DRY -eq 1 ]] && echo "ok True" || marts_state)
  [[ "$st" == "ok True" ]] && good "API marts_state/duckdb = $st  (was: $BASE_API)" || bad "API marts_state/duckdb = $st -- predictions still disabled (was: $BASE_API)"
  local newest; newest=$(qdb "select datediff('s', max(timestamp), now()) from bus_positions" | python3 -c "import json,sys; print(json.load(sys.stdin)['dataset'][0][0])" 2>/dev/null)
  [[ -n "$newest" && "$newest" -lt 300 ]] 2>/dev/null && good "ingest is live (newest row ${newest}s old)" || warn "newest bus_positions row is ${newest:-unknown}s old"
}

# ---- summary ----------------------------------------------------------------
summary() {
  major "SUMMARY  (total $(elapsed "$SCRIPT_START"))"
  local limit_gb; limit_gb=$(kb_to_gb "$MEMORY_LIMIT_KB")
  printf '  %-34s %s\n' "Run 1 (SKIP_EXTRACT=1)" "exit ${RUN1_RC:-not run}, ${RUN1_WALL:-n/a}, ${RUN1_PASS:-no dbt summary}"
  printf '  %-34s %s\n' "  peak RSS (largest process)" "$(kb_to_gb "${RUN1_PEAK_KB:-0}")GB   vs DuckDB memory_limit ${limit_gb}GB"
  printf '  %-34s %s\n' "Run 2 (systemd, full)" "exit ${RUN2_RC:-not run}"
  printf '  %-34s %s\n' "  peak RSS, largest / all together" "$(kb_to_gb "${RUN2_ONE_KB:-0}")GB / $(kb_to_gb "${RUN2_TOT_KB:-0}")GB"
  printf '  %-34s %s\n' "  cgroup peak (incl. page cache)" "${RUN2_CG_PEAK:-n/a}   vs MemoryMax $(grep -m1 '^MemoryMax' "$UNIT_SRC" | cut -d= -f2)"
  echo
  if ((${#PROBLEMS[@]})); then
    echo "  RESULT: FAIL -- do not push"; for p in "${PROBLEMS[@]}"; do echo "    - $p"; done
    [[ ${RUN1_OOM:-0} -gt 0 ]] && echo "  Note: DuckDB ran out of its own memory_limit. The peak RSS above is what to size it from."
    return 1
  fi
  echo "  RESULT: PASS -- both runs clean, marts republished, API healthy. Safe to push."
}

main() {
  if [[ $CHECK -eq 1 ]]; then
    preflight; echo; echo "  --check: preflight only. Nothing stopped, no log written. Run for real with:  sudo $0"; exit 0
  fi
  [[ $DRY -eq 1 || $EUID -eq 0 ]] || { echo "run with sudo:  sudo $0   (or --check / --dry-run without it)"; exit 1; }
  setup_log
  trap cleanup EXIT
  trap 'exit 130' INT TERM
  echo "verify_build.sh  $(date -u '+%F %T UTC')   log: $MAIN_LOG"
  [[ $DRY -eq 1 ]] && echo "*** DRY RUN: fake builds, nothing is stopped or started ***"
  preflight
  free_memory
  reclaim_swap
  local ok=1
  run1 || ok=0
  if [[ $ok -eq 1 ]]; then run2 || ok=0; else minor "skipping Run 2: Run 1 did not pass"; fi
  health
  summary; local rc=$?
  minor "log: $MAIN_LOG"
  exit $rc
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main; fi
