"""Publish per-line punctuality and ETA model-fit summaries into QuestDB.

Run by build.sh right after the marts are published (mv'd to
wraith_bus_ingestor.duckdb), so this reads the same file the API now
serves -- one authoritative snapshot, not the scratch build warehouse.

QuestDB rather than the duckdb marts themselves, same reason as
publish_run_results.py: duckdb has no wire protocol Grafana can reach.
line_punctuality and eta_models are per (line, direction, day_type,
hour_band) grain -- too fine to plot directly -- so this collapses each
to one row per line (punctuality) or one row overall (model fit) before
writing. That collapse is an approximation, not a re-derivation from raw
per-journey delays (BODS/this pipeline never stores an actual per-journey
delay, only the grain-level median/p90 dbt already computed), and the
per-line comment below says exactly what it approximates.
"""

import os
import sys
import urllib.request
from datetime import UTC, datetime

QUESTDB_HTTP = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
QUESTDB_ILP_URL = QUESTDB_HTTP.rstrip("/") + "/write"

DEFAULT_MARTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wraith_bus_ingestor.duckdb"
)

# DfT's Traffic Commissioner standard for local bus services: a bus is "on
# time" if it departs no more than 1 minute early and arrives no more than
# 5 minutes 59 seconds late. 95% on-time is the statutory target operators
# are held to. https://www.stoneking.co.uk/literature/e-bulletins/punctuality-and-reliability-bus-services-what-are-rules-and-what-traffic
ON_TIME_MIN_S = -60
ON_TIME_MAX_S = 359

# A model this weak isn't wrong to publish (eta_models' own fallback chain
# already handles a bad regression -- see docs/ROADMAP.md 1.0.0), but it is
# worth knowing how much of the ETA surface is running on a weak fit rather
# than a fitted one. 0.5 is a chosen cutoff, not a standard, and is named
# as such rather than implied to be authoritative.
WEAK_R2_CUTOFF = 0.5


def esc_tag(val):
    return str(val).replace(" ", "_").replace(",", "_").replace("=", "_")


def build_lines(con):
    """Turn the two duckdb marts into ILP lines. Kept separate from the
    connection/POST so it can be exercised against any duckdb connection,
    live or a fixture, without a real QuestDB."""
    ts = int(datetime.now(UTC).timestamp() * 1_000_000_000)
    lines = []

    per_line = con.execute(
        """
        select
            line,
            sum(n_journeys) as n_journeys,
            sum(case when median_delay_s between ? and ? then n_journeys else 0 end)
                as on_time_journeys,
            sum(median_delay_s * n_journeys) / sum(n_journeys) as median_delay_s,
            sum(p90_delay_s * n_journeys) / sum(n_journeys) as p90_delay_s
        from line_punctuality
        group by line
        """,
        [ON_TIME_MIN_S, ON_TIME_MAX_S],
    ).fetchall()
    for line, n_journeys, on_time, median_s, p90_s in per_line:
        tags = f"line={esc_tag(line)}"
        fields = (
            f"n_journeys={int(n_journeys)}i,on_time_journeys={int(on_time)}i,"
            f"median_delay_s={float(median_s)},p90_delay_s={float(p90_s)}"
        )
        lines.append(f"line_punctuality_summary,{tags} {fields} {ts}")

    avg_r2, weak_pct, avg_speed, n_models = con.execute(
        f"""
        select
            avg(r2),
            100.0 * sum(case when r2 < {WEAK_R2_CUTOFF} then 1 else 0 end) / count(*),
            avg(mean_speed_kmh),
            count(*)
        from eta_models
        """
    ).fetchone()
    fields = (
        f"avg_r2={float(avg_r2)},pct_weak_fit={float(weak_pct)},"
        f"avg_speed_kmh={float(avg_speed)},n_models={int(n_models)}i"
    )
    lines.append(f"eta_model_summary {fields} {ts}")
    return lines


def publish(marts_path):
    import duckdb

    con = duckdb.connect(marts_path, read_only=True)
    try:
        lines = build_lines(con)
    finally:
        con.close()
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(QUESTDB_ILP_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as response:
        return len(lines), response.status


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MARTS
    # Same rule as publish_run_results.py: a dashboard missing a datapoint is
    # a much smaller problem than a nightly build dying because a metrics
    # sink was unreachable, so every failure here degrades to a warning.
    try:
        count, status = publish(path)
        print(f"published {count} lines to {QUESTDB_ILP_URL} (questdb {status})")
    except Exception as exc:
        print(f"WARNING: could not publish punctuality summary: {exc}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
