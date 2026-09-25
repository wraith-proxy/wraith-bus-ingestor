"""Publish marts_state health into QuestDB so Grafana can alert on it.

The marts-state API (slaycoach.quest) reports whether the duckdb marts are
readable ('ok'), locked by a concurrent writer, missing, unreadable, or
lacking a driver. QuestDB has no way to poll that API itself, and duckdb
has no wire protocol Grafana can reach directly, so this script is the
bridge: fetch the API's verdict, write one row to QuestDB, and let systemd's
exit-code tracking surface anything other than 'ok' in the logs.

Unlike dbt/publish_run_results.py, which republishes a timestamp already
recorded in run_results.json, the marts-state API carries no timestamp of
its own -- so this script stamps the row with wall-clock now at publish time.
An unreachable API is itself the signal this exists to catch, so a failed
GET still publishes a row (state="unreachable", n_models=0) rather than
skipping the publish.
"""

import json
import os
import sys
import time
import urllib.request

MARTS_STATE_URL = os.environ.get(
    "MARTS_STATE_URL", "https://slaycoach.quest/api/model-info"
)
QUESTDB_HTTP = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
# ILP over HTTP lives on the REST port (/write); 9009 is the raw TCP socket
QUESTDB_ILP_URL = QUESTDB_HTTP.rstrip("/") + "/write"


def esc_tag(val):
    """ILP tag values cannot carry spaces, commas or equals signs unescaped."""
    return val.replace(" ", "_").replace(",", "_").replace("=", "_")


def fetch_state():
    """Return (state, n_models). Any failure degrades to ("unreachable", 0)
    rather than raising, since the API being down is itself worth a row --
    but the real exception is printed first (see main()'s caller), because
    silently collapsing every failure into "unreachable" hid a real bug: no
    User-Agent header meant a bare `python3 ops/publish_marts_state.py` got
    a 403 from whatever's in front of the public URL (Cloudflare or nginx
    treating urllib's default `Python-urllib/x.y` UA as a bot), while a
    plain `curl` to the exact same URL passed -- caught 2026-09-24, after
    the timer had been "working" (publishing a row every 5 minutes, just
    always the wrong one) since it was first enabled."""
    try:
        req = urllib.request.Request(
            MARTS_STATE_URL, method="GET",
            headers={"User-Agent": "wraith-bus-ingestor-marts-state-publisher"})
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status != 200:
                return "unreachable", 0
            payload = json.loads(response.read())
        state = payload["marts_state"]
        n_models = int(payload.get("n_models") or 0)
        return state, n_models
    except Exception as exc:
        print(f"WARN: could not fetch {MARTS_STATE_URL}: {exc}", file=sys.stderr)
        return "unreachable", 0


def build_line(state, n_models, ts_ns):
    """Turn a state reading into a single ILP line. Kept separate from the
    POST so it can be exercised without a live QuestDB."""
    tags = f"state={esc_tag(str(state))}"
    return f"marts_state,{tags} n_models={int(n_models)}i {ts_ns}"


def publish(line):
    body = (line + "\n").encode()
    req = urllib.request.Request(QUESTDB_ILP_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status


def main():
    state, n_models = fetch_state()
    ts_ns = time.time_ns()
    line = build_line(state, n_models, ts_ns)

    try:
        status = publish(line)
    except Exception as exc:
        print(f"ERROR: could not publish marts_state to QuestDB: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"published marts_state={state} n_models={n_models} to {QUESTDB_ILP_URL} (questdb {status})")
    sys.exit(0 if state == "ok" else 1)


if __name__ == "__main__":
    main()
