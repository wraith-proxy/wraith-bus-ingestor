#!/usr/bin/env python
"""Incident recovery for bus_positions: restore an archived day back into
live QuestDB, or replay a whole day range through extract+dbt.

Runs on the host (not in a container) -- it needs both `docker` (to reach
into questdb's uid-10001-owned partition dir, same workaround used for
server.conf edits) and this repo's own dbt/.venv (to reuse extract.py's
already-tested chunked-fetch logic rather than reimplementing it).

    ops/incident.py restore 2026-08-02
    ops/incident.py replay 2026-08-01 2026-08-03

restore is idempotent: a day already attached is a no-op, not an error.
replay is built on restore, not a copy of it -- each day in range gets
restore()'d if it's outside the hot RETENTION_DAYS window, then extracted
via the same fetch() extract.py's own nightly run uses, then one
SKIP_EXTRACT=1 FULL_REFRESH=1 build.sh call covers the whole range.

replay only helps for days that still exist in cold storage. A day lost
INSIDE the hot RETENTION_DAYS window cannot be recovered by anything here:
extract_day() re-reads that day from QuestDB, which is exactly where the
rows are already missing, and BODS serves live positions only -- there is
no backfill API to go back to. That case needs the archive, or it is gone.

Depends on bus_positions' DEDUP UPSERT KEYS(bus_id, timestamp) (enabled
2026-09-15, see docs/ROADMAP.md 1.3.0) -- without it, replaying a day that
was only partially lost (not fully missing) double-counts the rows that
were already there.

The archive is Parquet (docs/adr/0007): one self-describing file per day in
`${ARCHIVE_PATH}/bus_positions_parquet/<day>.parquet`, real strings and its
own schema. restore() copies the file into QuestDB's import directory and
INSERTs it back through read_parquet(); the archiver then finds the day already
archived, checks the file against the live rows, and drops the live copy. A
day can also be read in place, no restore needed:

    duckdb -c "SELECT ... FROM read_parquet('<archive>/bus_positions_parquet/*.parquet')"

63 days (2026-07-03 .. 2026-09-06) are numeric-only: their bus_id/line symbol
maps were lost in the 2026-09-18 schema migration (docs/adr/0006). They were
salvaged to Parquet with `timestamp lat lon bearing` and the raw integer keys,
and restore() refuses them -- there is no bus_id to load into bus_positions.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBT_DIR = os.path.join(REPO_ROOT, "dbt")
sys.path.insert(0, DBT_DIR)

QUESTDB = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
QUESTDB_CONTAINER = os.environ.get("QUESTDB_CONTAINER", "questdb")
TABLE = "bus_positions"
ARCHIVE_ROOT = os.environ.get("ARCHIVE_PATH", "/mnt/archive/wraith-bus-ingestor-archive")
# QuestDB keeps 3 UTC days (the archiver's RETENTION_DAYS); keep in step, .env is not read here.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "3"))


def qdb_exec(query, timeout=30):
    url = QUESTDB + "/exec?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def is_attached(day, table=TABLE):
    resp = qdb_exec(
        f"SELECT name FROM table_partitions('{table}') "
        f"WHERE attached = true AND name = '{day}'"
    )
    return len(resp.get("dataset", [])) > 0


PARQUET_DIR = os.path.join(ARCHIVE_ROOT, "bus_positions_parquet")
COLUMNS = [
    "bus_id", "lat", "lon", "bearing", "timestamp", "line", "operator", "direction",
    "journey_ref", "origin_aimed_dep", "published_line", "origin_ref", "origin_name",
    "dest_ref", "dest_name", "block_ref", "dest_aimed_arr", "recorded_at",
]  # bus_positions' columns in table order (docs/adr/0006)


def parquet_info(path):
    """(column names, row count) read from the file itself."""
    import duckdb

    con = duckdb.connect()
    try:
        probe = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [path])
        cols = [d[0] for d in probe.description]
        rows = con.execute("SELECT count(*) FROM read_parquet(?)", [path]).fetchone()[0]
    finally:
        con.close()
    return cols, rows


def restore(day):
    """Bring a day back from the Parquet archive into live bus_positions.
    No-op if already present. The row count is checked against the file."""
    if is_attached(day):
        print(f"{day}: already attached, nothing to do")
        return

    src = os.path.join(PARQUET_DIR, f"{day}.parquet")
    if not os.path.isfile(src):
        raise RuntimeError(f"no parquet archive for {day} at {src}")
    cols, rows = parquet_info(src)
    if "bus_id" not in cols:
        raise RuntimeError(
            f"{day}: numeric-only archive (salvaged from a pre-2026-09-18 partition whose "
            "bus_id/line symbol maps were lost, docs/adr/0006) -- there is no bus_id to load "
            f"into {TABLE}. Read it in place: duckdb -c \"SELECT * FROM read_parquet('{src}')\""
        )

    name = f"restore_{day}.parquet"
    in_container = f"/var/lib/questdb/import/{name}"
    print(f"{day}: copying {src} -> {QUESTDB_CONTAINER}:{in_container} ({rows:,} rows)")
    docker("cp", src, f"{QUESTDB_CONTAINER}:{in_container}")
    try:
        docker("exec", "-u", "root", QUESTDB_CONTAINER, "chown", "10001:10001", in_container)
        cols_sql = ", ".join(COLUMNS)
        resp = qdb_exec(
            f"INSERT INTO {TABLE} ({cols_sql}) SELECT {cols_sql} FROM read_parquet('{name}')",
            timeout=900,
        )
        if "ddl" not in resp and "dml" not in resp:
            raise RuntimeError(f"{day}: INSERT from parquet failed: {resp!r}")
        for _ in range(600):  # WAL apply is asynchronous
            got = qdb_exec(f"SELECT count() FROM {TABLE} WHERE timestamp IN '{day}'")
            got = got["dataset"][0][0]
            if got == rows:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"{day}: restored {got:,} rows, the file has {rows:,}")
    finally:
        docker("exec", "-u", "root", QUESTDB_CONTAINER, "rm", "-f", in_container, check=False)
    print(f"{day}: restored {rows:,} rows")


def extract_day(day_obj):
    """Pull one day using extract.py's own build_select/fetch -- same
    chunking, same column typing, same empty-file handling as the nightly
    run. Reimporting rather than reimplementing: this is the one place that
    logic should live."""
    import duckdb
    import extract

    select, columns = extract.build_select(extract.questdb_columns())
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # extract.py's main() does the same before its chunk loop: the pre-chunking
    # single-file name joins the same glob as the 4-hour chunks, so leaving one
    # behind double-counts the day.
    stale = os.path.join(extract.OUT, f"bus_positions_{day_obj.isoformat()}.parquet")
    if os.path.exists(stale):
        os.remove(stale)
    # The day's .done marker goes before the re-fetch, so a re-fetch that dies
    # midway leaves the day unmarked rather than marked over half-replaced
    # chunks. Nothing here writes it back: if the day is still in the extract
    # window, the next nightly fetches it again and marks it by its own rule
    # (rows, WAL applied).
    done = os.path.join(extract.OUT, f"bus_positions_{day_obj.isoformat()}.done")
    if os.path.exists(done):
        os.remove(done)
    total = 0
    for hour_start in range(0, 24, extract.CHUNK_HOURS):
        dest, n = extract.fetch(
            con, day_obj, hour_start, hour_start + extract.CHUNK_HOURS, select, columns
        )
        total += n
    con.close()
    print(f"{day_obj.isoformat()}: extracted {total:,} rows")
    return total


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def replay(start_str, end_str):
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    if start > end:
        raise RuntimeError(f"start {start} is after end {end}")

    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    for d in daterange(start, end):
        if d < cutoff:
            restore(d.isoformat())
        extract_day(d)

    print(f"==> re-running dbt for {start}..{end} (SKIP_EXTRACT=1 FULL_REFRESH=1)")
    env = dict(os.environ, SKIP_EXTRACT="1", FULL_REFRESH="1", REPLAY="1")
    subprocess.run(["./build.sh"], cwd=DBT_DIR, env=env, check=True)


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("restore", help="load one archived day back from its Parquet file")
    r.add_argument("day", help="YYYY-MM-DD")

    rp = sub.add_parser(
        "replay", help="restore (if needed) + re-extract + full-refresh dbt over a day range"
    )
    rp.add_argument("start", help="YYYY-MM-DD")
    rp.add_argument("end", help="YYYY-MM-DD")

    args = p.parse_args()
    if args.cmd == "restore":
        restore(args.day)
    elif args.cmd == "replay":
        replay(args.start, args.end)


if __name__ == "__main__":
    main()
