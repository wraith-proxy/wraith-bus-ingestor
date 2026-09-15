"""Publish dbt run results into QuestDB so Grafana has build health to plot.

Run by dbt/build.sh after every dbt invocation, pass or fail. dbt writes
target/run_results.json on every run, so that file -- not build.log -- is the
input: it is structured, versioned and produced even when the run fails.
Two measurements come out of it: dbt_test_results (one row per test) and
dbt_build_runs (one row per run).

QuestDB rather than the duckdb marts because duckdb has no wire protocol
Grafana can reach -- it is an embedded file opened by whoever holds the lock,
so a dashboard cannot read it at all while a build is writing it. QuestDB is
already the ingest sink and already speaks HTTP, so Grafana just points at it.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime

QUESTDB_HTTP = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
# ILP over HTTP lives on the REST port (/write); 9009 is the raw TCP socket
QUESTDB_ILP_URL = QUESTDB_HTTP.rstrip("/") + "/write"

DEFAULT_RESULTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "target", "run_results.json"
)


def esc_tag(val):
    """ILP tag values cannot carry spaces, commas or equals signs unescaped."""
    return val.replace(" ", "_").replace(",", "_").replace("=", "_")


def epoch_ns(stamp):
    """dbt stamps are ISO8601 with a trailing Z, which fromisoformat wants as
    an explicit offset before Python 3.11."""
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1_000_000_000)


def build_lines(run_results):
    """Turn a parsed run_results.json into ILP lines. Kept separate from the
    POST so it can be exercised without a live QuestDB."""
    metadata = run_results.get("metadata") or {}
    results = run_results.get("results") or []
    # Every row carries the time of the run it describes, not wall-clock-now,
    # so a late or replayed publish still lands on the right point in Grafana.
    ts = epoch_ns(metadata["generated_at"])

    tests = [r for r in results if str(r.get("unique_id") or "").startswith("test.")]
    models = sum(1 for r in results if str(r.get("unique_id") or "").startswith("model."))

    lines = []
    for r in tests:
        status = str(r.get("status") or "")
        failures = int(r.get("failures") or 0)
        exec_ms = float(r.get("execution_time") or 0.0) * 1000
        tags = f"test={esc_tag(str(r['unique_id']))},status={esc_tag(status)}"
        lines.append(f"dbt_test_results,{tags} failures={failures}i,exec_ms={exec_ms} {ts}")

    passed = sum(1 for r in tests if str(r.get("status") or "") == "pass")
    failed = sum(1 for r in tests if str(r.get("status") or "") in ("fail", "error"))
    warned = sum(1 for r in tests if str(r.get("status") or "") == "warn")
    elapsed = metadata.get("elapsed_time")
    if elapsed is None:
        elapsed = sum(float(r.get("execution_time") or 0.0) for r in results)

    result = "fail" if failed else "success"
    fields = (
        f"elapsed_s={float(elapsed)},models={models}i,"
        f"tests_passed={passed}i,tests_failed={failed}i,tests_warned={warned}i"
    )
    lines.append(f"dbt_build_runs,result={result} {fields} {ts}")
    return lines


def publish(path):
    with open(path) as fh:
        run_results = json.load(fh)
    lines = build_lines(run_results)
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(QUESTDB_ILP_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as response:
        return len(lines), response.status


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RESULTS
    # A dashboard that misses a datapoint is a much smaller problem than a
    # nightly build that dies because its metrics sink was down, so every
    # failure here degrades to a warning and a zero exit.
    try:
        count, status = publish(path)
        print(f"published {count} lines to {QUESTDB_ILP_URL} (questdb {status})")
    except Exception as exc:
        print(f"WARNING: could not publish dbt run results: {exc}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
