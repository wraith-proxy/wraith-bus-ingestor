#!/usr/bin/env bash
#
# Nightly host reclaim, run 02:00 -- one hour ahead of wraith-bus-build's
# 03:00 -- so a memory- and disk-tight host starts the actual dbt rebuild
# from a clean slate instead of fighting whatever the day left behind.
#
# Two, unrelated problems this closes, both from docs/ROADMAP.md's "Known
# issues": swap that accumulates during the day never clears on its own
# (a freed container's pages go back to page cache, not free memory, and
# swapped-out pages stay swapped until touched again -- observed directly
# 2026-09-14 and again 2026-09-18, see ops/systemd/wraith-bus-build.service's
# own MemoryMax history); and Docker images/build cache that pile up from
# CI/local work and are never anyone's job to clear.
#
# Every step here is soft-fail: this script's job is to make tonight's
# build easier, never to block it. A step that can't run safely logs why
# and moves on. `set -e` is deliberately NOT used for that reason -- see
# each step's own guard.
#
set -uo pipefail

LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"
log() { echo "$LOG_PREFIX $*"; }

log "==> precleanup starting"

# ---------------------------------------------------------------------------
# Swap reclaim -- one swapfile at a time, only if it actually fits.
#
# swapoff has to read every swapped-out page back into real RAM before it
# can drop the swapfile, so it needs roughly that swapfile's `Used` bytes
# of headroom. Checked per-file rather than once for both combined: this
# host's two swapfiles are 4G and 16G, and forcing both back at once on a
# host that's merely "a bit tight" (not clear) is exactly the kind of
# unconfined, unmeasured operation this project's own incident history
# (docs/adr/, the 2026-09-14 host crash) says not to run. A skipped file
# tonight just means less reclaimed, not a failure -- it'll get another
# chance tomorrow night once whatever's holding it has actually cleared.
# ---------------------------------------------------------------------------
reclaim_swap() {
    local swapfile="$1"
    local used_kb avail_kb margin_kb

    used_kb=$(awk -v f="$swapfile" '$1==f {print $4}' /proc/swaps)
    if [[ -z "$used_kb" ]]; then
        log "swap: $swapfile not active, skipping"
        return 0
    fi
    if [[ "$used_kb" -eq 0 ]]; then
        log "swap: $swapfile already at 0 used, nothing to reclaim"
        return 0
    fi

    avail_kb=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
    # Require the swapped bytes PLUS a 1GB safety margin to actually be
    # available before attempting -- not just >=, since MemAvailable's own
    # estimate is an approximation and other processes keep allocating
    # while this runs.
    margin_kb=$((1024 * 1024))
    if (( avail_kb < used_kb + margin_kb )); then
        log "swap: $swapfile has ${used_kb}KB used, only ${avail_kb}KB available (need ${used_kb}+${margin_kb}KB) -- skipping, not safe tonight"
        return 0
    fi

    log "swap: reclaiming $swapfile (${used_kb}KB used, ${avail_kb}KB available)"
    if swapoff "$swapfile" 2>&1 | while read -r line; do log "swap: $line"; done; then
        swapon "$swapfile" 2>&1 | while read -r line; do log "swap: $line"; done
        log "swap: $swapfile reclaimed and re-enabled"
    else
        log "swap: swapoff $swapfile failed -- leaving as-is, re-enabling to be safe"
        swapon "$swapfile" 2>&1 | while read -r line; do log "swap: $line"; done
    fi
}

if [[ $EUID -ne 0 ]]; then
    log "swap: not running as root, skipping reclaim entirely (see service User=)"
else
    # Smallest first: same reasoning as the manual run this was based on --
    # prove the margin on the cheap file before trying the expensive one.
    while read -r swapfile _; do
        [[ "$swapfile" == Filename* ]] && continue
        reclaim_swap "$swapfile"
    done < <(sort -k4 -n /proc/swaps)
fi

# ---------------------------------------------------------------------------
# Docker artifact prune -- dangling images and unused build cache only.
# Deliberately NOT `-a` on either: this host runs ~25 other unrelated
# containers (immich, ollama, gitea, ...) and a blanket prune would take
# out images they need between runs, not just this project's own leftovers.
# Both commands below only ever touch things nothing references.
# ---------------------------------------------------------------------------
log "==> docker image prune (dangling only)"
docker image prune -f 2>&1 | while read -r line; do log "docker: $line"; done

log "==> docker builder prune (unused cache only)"
docker builder prune -f 2>&1 | while read -r line; do log "docker: $line"; done

log "==> precleanup finished"
exit 0
