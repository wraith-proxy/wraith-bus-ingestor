#!/usr/bin/env bash
# Stop optional containers for the nightly build window, restart exactly those
# afterwards. Called from wraith-bus-build.service:
#
#   ExecStartPre=-ops/build_window.sh stop  ollama immich_server ...
#   ExecStopPost=-ops/build_window.sh start
#
# Why a script and not two `docker stop/start` lines: a blind `docker start`
# would also start a container you had deliberately left off. `stop` records
# what it actually stopped in $STATE (the unit's RuntimeDirectory, deleted when
# the unit ends); `start` restarts only that, in reverse order, so dependencies
# stop last and start first. ExecStopPost runs when the build fails too.
# Never fails the build: a missing container or a failed start is logged and
# skipped.
set -uo pipefail

STATE="${WRAITH_BUILD_WINDOW_STATE:-/run/wraith-bus-build/stopped}"
mode="${1:-}"; shift || true

case "$mode" in
  stop)
    # No state file, no stopping: without a record of what we stopped, `start`
    # could never bring it back.
    if ! { mkdir -p "$(dirname "$STATE")" && : > "$STATE"; } 2>/dev/null; then
      echo "build window: cannot write $STATE -- stopping nothing" >&2; exit 0
    fi
    for c in "$@"; do
      if [[ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" == "true" ]]; then
        # recorded BEFORE stopping: a crash in between then leaves a running
        # container listed (a harmless `docker start`), never a stopped one forgotten
        echo "$c" >> "$STATE" && docker stop "$c" >/dev/null && echo "build window: stopped $c"
      else
        echo "build window: $c not running, leaving it"
      fi
    done ;;
  start)
    [[ -s "$STATE" ]] || { echo "build window: nothing to restart"; exit 0; }
    while read -r c; do
      docker start "$c" >/dev/null && echo "build window: restarted $c" || echo "build window: FAILED to restart $c"
    done < <(tac "$STATE")
    : > "$STATE" ;;
  *) echo "usage: $0 stop <container...> | start" >&2; exit 2 ;;
esac
exit 0
