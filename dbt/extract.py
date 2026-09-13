#!/usr/bin/env python
"""Pull a bounded window of bus_positions out of QuestDB into parquet for dbt.

QuestDB holds ~1e9 rows; dbt only needs enough to fit per-line models, so we
downsample to one row per bus per SAMPLE_MINS and keep the last EXTRACT_DAYS
days. One parquet file per day, so a re-run only redoes the days it is asked for.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

QUESTDB = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
DAYS = int(os.environ.get("EXTRACT_DAYS", "7"))
SAMPLE_MINS = int(os.environ.get("EXTRACT_SAMPLE_MINS", "3"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Columns we take verbatim, and columns we take the last value of within a bucket.
KEYS = ["timestamp", "bus_id", "line"]
WANTED = [
    "lat", "lon", "bearing", "direction", "operator", "journey_ref",
    "origin_ref", "origin_name", "dest_ref", "dest_name",
    "origin_aimed_dep", "dest_aimed_arr",
]
# Pin every column's type. Left to auto-detect, a day whose schedule columns are
# entirely empty types them VARCHAR while a day that has values types them
# BIGINT, and read_parquet(..., union_by_name=true) then has to reconcile
# VARCHAR with BIGINT across the glob. Same story for a zero-row day, which
# auto-detects as VARCHAR right down to the timestamp.
TYPES = {
    "timestamp": "TIMESTAMP WITH TIME ZONE",
    "bus_id": "VARCHAR", "line": "VARCHAR",
    "lat": "DOUBLE", "lon": "DOUBLE", "bearing": "DOUBLE",
    "direction": "VARCHAR", "operator": "VARCHAR", "journey_ref": "VARCHAR",
    "origin_ref": "VARCHAR", "origin_name": "VARCHAR",
    "dest_ref": "VARCHAR", "dest_name": "VARCHAR",
    "origin_aimed_dep": "BIGINT", "dest_aimed_arr": "BIGINT",
}


def questdb_columns():
    """The schedule columns only appear once the patched ingestor has run, so
    ask QuestDB what it actually has rather than guessing."""
    q = urllib.parse.quote("SELECT * FROM bus_positions LIMIT 0")
    with urllib.request.urlopen(f"{QUESTDB}/exec?query={q}", timeout=30) as r:
        return {c["name"] for c in json.load(r)["columns"]}


def build_select(available):
    cols = [c for c in WANTED if c in available]
    missing = [c for c in WANTED if c not in available]
    if missing:
        print(f"not yet in QuestDB, skipping: {', '.join(missing)}")
    select = ", ".join(KEYS + [f"last({c}) {c}" for c in cols])
    columns = "{" + ", ".join(f"'{c}': '{TYPES[c]}'" for c in KEYS + cols) + "}"
    return select, columns


def fetch(con, day, select, columns):
    query = (
        f"SELECT {select} FROM bus_positions "
        f"WHERE timestamp IN '{day.isoformat()}' SAMPLE BY {SAMPLE_MINS}m"
    )
    url = f"{QUESTDB}/exp?" + urllib.parse.urlencode({"query": query})
    dest = os.path.join(OUT, f"bus_positions_{day.isoformat()}.parquet")
    # COPY ... TO takes no bind parameters, so quote the literals ourselves.
    src, tgt = url.replace("'", "''"), dest.replace("'", "''")
    con.execute(
        f"COPY (SELECT * FROM read_csv('{src}', header=true, columns={columns})) "
        f"TO '{tgt}' (FORMAT PARQUET, COMPRESSION zstd)"
    )
    n = con.execute("SELECT count(*) FROM read_parquet(?)", [dest]).fetchone()[0]
    if n == 0:
        # The ingestor was down that day. An empty file is worse than no file:
        # it still joins the glob and contributes nothing but a schema to argue
        # with, so drop it and let the gap be a gap.
        os.remove(dest)
        return None, 0
    return dest, n


def main():
    import duckdb

    os.makedirs(OUT, exist_ok=True)
    select, columns = build_select(questdb_columns())
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    total, gaps = 0, []
    for i in range(DAYS, 0, -1):
        day = date.today() - timedelta(days=i)
        dest, n = fetch(con, day, select, columns)
        total += n
        if dest is None:
            gaps.append(day)
            print(f"{day} -> NO DATA (ingest gap), no file written")
        else:
            print(f"{day} -> {os.path.basename(dest)} ({n:,} rows)")
    con.close()
    if gaps:
        print(f"warning: {len(gaps)} day(s) with no data: "
              + ", ".join(f"{d} ({d:%a})" for d in gaps))
    print(f"total {total:,} rows in {OUT}")
    assert total > 0, "extract produced no rows -- is QuestDB reachable?"


if __name__ == "__main__":
    sys.exit(main())
