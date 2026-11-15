#!/usr/bin/env bash
# Spike #54 (c): MetricFlow peak memory on a COPY of the real build.
#
#   ops/spike_mf_memory.sh [--dry-run]      (WORK=dir to change the work dir)
#
# Copies dbt/ plus $BUILD into $WORK, adds a time spine and a constant time
# dimension to the COPY ONLY (MetricFlow needs both, the repo ships neither:
# ADR-0011 note), then runs `mf validate-configs` and a few `mf query` calls as
# a systemd user service capped at MemoryMax. Records cgroup MemoryPeak and
# per-query max RSS. Never touches $BUILD, the served file, the repo or any
# container. Needs no sudo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$REPO/dbt/wraith_bus_ingestor_build.duckdb"
WORK="${WORK:-$HOME/spike-mf-memory}"
MEMMAX="${MEMMAX:-13000M}"
MIN_FREE_GB=20
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
LOG="$WORK.log"
UNIT="spike-mf-memory"

say() { echo "==> $(date +%T) $*" | tee -a "$LOG"; }
run() { echo "+ $*" | tee -a "$LOG"; [[ $DRY == 1 ]] || "$@" >>"$LOG" 2>&1; }
die() { echo "!! $*" | tee -a "$LOG" >&2; exit 1; }

say "stage 0: checks (dry-run=$DRY, log=$LOG)"
case "$WORK" in "$REPO"|"$REPO"/*|/tmp|/tmp/*|""|"/"|"$HOME") die "WORK must be outside the repo and /tmp: $WORK" ;; esac
[[ -f "$BUILD" ]] || die "missing $BUILD"
free_gb=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc 0-9)
[[ $free_gb -ge $MIN_FREE_GB ]] || die "only ${free_gb}G free, need $MIN_FREE_GB"
if ${PGREP:-pgrep} -f "build.sh|dbt run|extract.py" >/dev/null; then die "a build or extract is running"; fi
hh=${NOW_HHMM:-$(date -u +%H%M)}; [[ $((10#$hh)) -lt 230 || $((10#$hh)) -gt 430 ]] || die "inside the nightly build window (02:30-04:30 UTC)"
say "ok: ${free_gb}G free, build $(du -h "$BUILD" | cut -f1)"

say "stage 1: scratch venv (dbt + metricflow, versions from the #54 feasibility check)"
run rm -rf "$WORK"
run mkdir -p "$WORK"
run uv venv -q --python 3.11 "$WORK/venv"
run env VIRTUAL_ENV="$WORK/venv" uv pip install -q dbt-core==1.12.4 dbt-duckdb==1.11.0 duckdb==1.5.5 dbt-metricflow
VBIN="$WORK/venv/bin"

say "stage 2: copy the project and the real build file"
run mkdir -p "$WORK/proj"
if [[ $DRY == 0 ]]; then
  tar -C "$REPO/dbt" --exclude='*.duckdb' --exclude='*.duckdb.wal' --exclude='data' --exclude='target' \
    --exclude='logs' --exclude='__pycache__' --exclude='*.log' --exclude='dbt_packages' -cf - . | tar -C "$WORK/proj" -xf -
  [[ -d "$REPO/dbt/dbt_packages" ]] && cp -r "$REPO/dbt/dbt_packages" "$WORK/proj/dbt_packages"
fi
run cp "$BUILD" "$WORK/proj/wraith_bus_ingestor_build.duckdb"

say "stage 3: add time spine + constant time dimension to the COPY (not the repo)"
if [[ $DRY == 0 ]]; then
  mkdir -p "$WORK/proj/models/semantic"
  cat > "$WORK/proj/models/semantic/metricflow_time_spine_day.sql" <<'EOF'
select cast(d as date) as date_day
from generate_series(date '2020-01-01', date '2035-12-31', interval 1 day) t(d)
EOF
  cat > "$WORK/proj/models/semantic/_time_spine.yml" <<'EOF'
models:
  - name: metricflow_time_spine_day
    time_spine:
      standard_granularity_column: date_day
    columns:
      - name: date_day
        granularity: day
EOF
  "$VBIN/python" - "$REPO/dbt/semantic/_semantic.yml" "$WORK/proj/models/semantic/_semantic.yml" <<'EOF'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
for s in d["semantic_models"]:
    s["dimensions"].append({"name": "const_day", "type": "time", "expr": "cast('2000-01-01' as date)",
                            "type_params": {"time_granularity": "day"}})
    s["defaults"] = {"agg_time_dimension": "const_day"}
yaml.safe_dump(d, open(sys.argv[2], "w"))
EOF
fi
export DBT_PROFILES_DIR="$WORK/proj"
run "$VBIN/dbt" run -s metricflow_time_spine_day --project-dir "$WORK/proj"
run "$VBIN/dbt" parse --project-dir "$WORK/proj"

say "stage 4: measure (service capped at MemoryMax=$MEMMAX, swap off)"
[[ $DRY == 1 ]] || cat > "$WORK/queries.sh" <<EOF
#!/usr/bin/env bash
cd "$WORK/proj"
n=0
q() { n=\$((n+1)); echo "--- q\$n: \$*"; /usr/bin/time -v -o "$WORK/time.\$n" "$VBIN/mf" "\$@"; echo "q\$n max RSS kB: \$(awk '/Maximum resident/{print \$NF}' "$WORK/time.\$n")"; }
q validate-configs
q query --metrics journey_count --group-by line_punctuality_key__line
q query --metrics eta_observations --group-by eta_models_key__operator
q query --metrics eta_observations --group-by eta_models_key__operator,eta_models_key__line,eta_models_key__direction,eta_models_key__hour_band
q query --metrics locality_count,locality_stop_count
EOF
[[ $DRY == 1 ]] || chmod +x "$WORK/queries.sh"
if [[ $DRY == 0 ]]; then
  systemctl --user stop "$UNIT" 2>/dev/null || true; systemctl --user reset-failed "$UNIT" 2>/dev/null || true
  systemd-run --user --unit="$UNIT" --wait --pipe -p MemoryMax="$MEMMAX" -p MemorySwapMax=0 \
    -E DBT_PROFILES_DIR="$DBT_PROFILES_DIR" "$WORK/queries.sh" 2>&1 | tee -a "$LOG" | tail -40 || true
  PEAK=$(grep -a "^Memory peak:" "$LOG" | tail -1 | cut -d: -f2 | tr -d ' ')  # systemd-run prints it; MemoryPeak is unset once the unit exits
  systemctl --user stop "$UNIT" 2>/dev/null || true; systemctl --user reset-failed "$UNIT" 2>/dev/null || true
  say "RESULT: cgroup peak memory = ${PEAK:-unknown}; per-query max RSS above"
else
  echo "+ systemd-run --user --wait --pipe -p MemoryMax=$MEMMAX -p MemorySwapMax=0 $WORK/queries.sh"
fi
say "done. Copy in $WORK is disposable: rm -rf $WORK"
