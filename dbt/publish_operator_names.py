"""Load noc_operator_names.csv into QuestDB so Grafana can show real
operator names instead of raw four-letter NOC codes.

Unlike publish_run_results.py and publish_punctuality_summary.py, this is
not run by build.sh on every build -- the data isn't build-derived, it's a
static reference table, checked into git as noc_operator_names.csv (a
curated extract of the real National Operator Code database,
https://www.travelinedata.org.uk/noc/api/1.0/nocrecords.xml, covering the
operators actually seen in this feed's own bus_positions -- not the full
~3,300-operator UK database, which would be a lot of dead weight for a
panel that only ever shows the ten busiest). Run by hand, occasionally,
when a new operator's code shows up unmapped on the dashboard:

    dbt/.venv/bin/python publish_operator_names.py    (or the repo's own
                                                         .venv, same as
                                                         every other
                                                         publish_*.py)

QuestDB rather than a dbt seed: dbt seeds load into the duckdb warehouse,
which Grafana cannot reach (no wire protocol) -- same reasoning as every
other publish_*.py script in this directory.
"""

import csv
import os
import sys
import urllib.request

QUESTDB_HTTP = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
QUESTDB_ILP_URL = QUESTDB_HTTP.rstrip("/") + "/write"

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "noc_operator_names.csv")


def esc_tag(val):
    return str(val).replace(" ", "_").replace(",", "_").replace("=", "_")


def esc_str(val):
    return str(val).replace("\\", "\\\\").replace('"', '\\"')


def build_lines(csv_path):
    lines = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = row["operator_code"].strip()
            name = row["operator_name"].strip()
            if not code or not name:
                continue
            lines.append(
                f'operator_names,operator_code={esc_tag(code)} '
                f'operator_name="{esc_str(name)}" 0'
            )
    return lines


def publish(csv_path):
    lines = build_lines(csv_path)
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(QUESTDB_ILP_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        return len(lines), response.status


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    try:
        count, status = publish(path)
        print(f"published {count} operator names to {QUESTDB_ILP_URL} (questdb {status})")
    except Exception as exc:
        print(f"WARNING: could not publish operator names: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
