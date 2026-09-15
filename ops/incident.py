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

Depends on bus_positions' DEDUP UPSERT KEYS(bus_id, timestamp) (enabled
2026-09-15, see docs/ROADMAP.md 1.3.0) -- without it, replaying a day that
was only partially lost (not fully missing) double-counts the rows that
were already there.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, UTC

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBT_DIR = os.path.join(REPO_ROOT, "dbt")
sys.path.insert(0, DBT_DIR)

QUESTDB = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
QUESTDB_CONTAINER = os.environ.get("QUESTDB_CONTAINER", "questdb")
TABLE = "bus_positions"
ARCHIVE_ROOT = os.environ.get("ARCHIVE_PATH", "/mnt/archive/wraith-bus-ingestor-archive")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "8"))


def qdb_exec(query):
    url = QUESTDB + "/exec?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def is_attached(day):
    resp = qdb_exec(
        f"SELECT name FROM table_partitions('{TABLE}') "
        f"WHERE attached = true AND name = '{day}'"
    )
    return len(resp.get("dataset", [])) > 0


def table_dir_name():
    """bus_positions~N -- discover it inside the container the same way
    archiver/archive.py does on the host side; the suffix is QuestDB's own
    and not something to hardcode."""
    out = docker(
        "exec", QUESTDB_CONTAINER, "sh", "-c",
        f"find /var/lib/questdb/db -maxdepth 1 -name '{TABLE}~*' -type d -printf '%f'",
    ).stdout.strip()
    if not out:
        raise RuntimeError(f"could not find {TABLE}~* dir inside {QUESTDB_CONTAINER}")
    return out


def restore(day):
    """ATTACH a day back from cold storage. No-op if already attached."""
    if is_attached(day):
        print(f"{day}: already attached, nothing to do")
        return

    src = os.path.join(ARCHIVE_ROOT, TABLE, day)
    if not os.path.isdir(src):
        raise RuntimeError(f"no archived partition for {day} at {src}")

    tdir = table_dir_name()
    dest_in_container = f"/var/lib/questdb/db/{tdir}/{day}.attachable"
    print(f"{day}: copying {src} -> {QUESTDB_CONTAINER}:{dest_in_container}")
    docker("cp", src, f"{QUESTDB_CONTAINER}:{dest_in_container}")
    docker("exec", "-u", "root", QUESTDB_CONTAINER, "chown", "-R", "10001:10001", dest_in_container)

    resp = qdb_exec(f"ALTER TABLE {TABLE} ATTACH PARTITION LIST '{day}'")
    if "ddl" not in resp:
        raise RuntimeError(f"ATTACH failed for {day}: {resp!r}")

    for _ in range(20):
        if is_attached(day):
            print(f"{day}: attached")
            return
        time.sleep(0.5)
    raise RuntimeError(f"{day}: ATTACH accepted but not visible as attached after 10s")


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
    total = 0
    for hour_start in range(0, 24, extract.CHUNK_HOURS):
        dest, n = extract.fetch(con, day_obj, hour_start, hour_start + extract.CHUNK_HOURS, select, columns)
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
    env = dict(os.environ, SKIP_EXTRACT="1", FULL_REFRESH="1")
    subprocess.run(["./build.sh"], cwd=DBT_DIR, env=env, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("restore", help="ATTACH one archived day back into live QuestDB")
    r.add_argument("day", help="YYYY-MM-DD")

    rp = sub.add_parser("replay", help="restore (if needed) + re-extract + full-refresh dbt over a day range")
    rp.add_argument("start", help="YYYY-MM-DD")
    rp.add_argument("end", help="YYYY-MM-DD")

    args = p.parse_args()
    if args.cmd == "restore":
        restore(args.day)
    elif args.cmd == "replay":
        replay(args.start, args.end)


if __name__ == "__main__":
    main()
