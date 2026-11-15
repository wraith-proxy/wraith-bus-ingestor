"""Load noc_operator_names.csv into QuestDB for Grafana operator names.

This lets Grafana show real
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

Environment:
    QUESTDB_HTTP: QuestDB HTTP base URL (default http://localhost:9001).

Exit codes:
    0 on success, 1 when the publish fails.
"""

import csv
import os
import sys
import urllib.request

QUESTDB_HTTP = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
QUESTDB_ILP_URL = QUESTDB_HTTP.rstrip("/") + "/write"

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "noc_operator_names.csv")


def esc_tag(val):
    """Escape a value for use as an ILP tag.

    Args:
        val: Value to escape; coerced to str.

    Returns:
        The string with spaces, commas and equals signs replaced by underscores.
    """
    return str(val).replace(" ", "_").replace(",", "_").replace("=", "_")


def esc_str(val):
    """Escape a value for use inside a quoted ILP string field.

    Args:
        val: Value to escape; coerced to str.

    Returns:
        The string with backslashes and double quotes escaped.
    """
    return str(val).replace("\\", "\\\\").replace('"', '\\"')


def build_lines(csv_path):
    """Build ILP lines from the operator names CSV, skipping incomplete rows.

    Args:
        csv_path: Path to a CSV with operator_code and operator_name columns.

    Returns:
        A list of ILP lines, one per row that has both a code and a name.
    """
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
    """POST the operator names to QuestDB over ILP/HTTP.

    Args:
        csv_path: Path to the operator names CSV.

    Returns:
        A tuple of (line count, HTTP status).

    Raises:
        urllib.error.URLError: QuestDB is unreachable or rejects the request.
    """
    lines = build_lines(csv_path)
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(QUESTDB_ILP_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        return len(lines), response.status


def main():
    """Publish the CSV named by argv[1], or the default CSV.

    Raises:
        SystemExit: Exit code 1 when the publish fails.
    """
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    try:
        count, status = publish(path)
        print(f"published {count} operator names to {QUESTDB_ILP_URL} (questdb {status})")
    except Exception as exc:
        print(f"WARNING: could not publish operator names: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
