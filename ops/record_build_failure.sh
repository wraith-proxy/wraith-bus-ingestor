#!/usr/bin/env bash
# Called from wraith-bus-build.service as ExecStopPost. If the unit did not end in
# success, write a 'failed' row to dbt_build_runs so the Grafana rule
# "nightly build missing or failed" (last(result) != 'success') fires within ~10
# minutes, instead of 26 hours after the last success. Until this existed, a run
# that died before dbt published its results (an OOM in `dbt run`, an extract
# failure) recorded nothing: 2026-09-20's failed 03:00 build looked fine to the
# owner because the API kept serving the previous good marts.
#
# systemd sets $SERVICE_RESULT for ExecStopPost ('success', 'exit-code', 'signal',
# 'timeout', ...). A later successful build inserts its own 'success' row, which
# resolves the alert. Never fails the unit: the `-` prefix in the unit, and
# every error here is swallowed.
set -uo pipefail

QUESTDB="${QUESTDB_HTTP:-http://localhost:9001}"
TABLE="${BUILD_RUNS_TABLE:-dbt_build_runs}"
result="${SERVICE_RESULT:-unknown}"

[[ "$result" == "success" ]] && exit 0

# columns: result, elapsed_s, models, tests_passed, tests_failed, tests_warned, timestamp
sql="INSERT INTO ${TABLE} VALUES('failed', 0, 0, 0, 0, 0, now())"
if curl -sf --max-time 15 -G "$QUESTDB/exec" --data-urlencode "query=$sql" >/dev/null; then
  echo "recorded failed build (unit result: $result)"
else
  echo "could not record the failed build in QuestDB (unit result: $result)" >&2
fi
exit 0
