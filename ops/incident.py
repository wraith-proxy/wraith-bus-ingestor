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

restore() ALSO handles days archived under the pre-2026-09-18 schema
(docs/adr/0006): bus_positions changed column types (vehicle_id dropped;
journey_ref/published_line/origin_ref/origin_name/dest_ref/dest_name/
block_ref VARCHAR -> SYMBOL; lat/lon/bearing DOUBLE -> FLOAT) and QuestDB
has no live cast-on-attach -- ATTACH PARTITION requires the on-disk file to
already match the live table's column definitions. An archived day whose
files still carry the dropped vehicle_id column is attached into a small
persistent legacy-schema shadow table instead (the layout it was archived
under), then INSERTed into live bus_positions through the same CAST()
expressions the bulk migration used. Rewritten lazily, one day at a time,
only for a day someone actually restores -- not an upfront rewrite of the
whole archive.

The test is the partition's own files, NOT the day's date: every day that
was in the live table on 2026-09-18 was migrated in place, so a day like
2026-09-08 archived AFTER the cutover has the new layout despite predating
it. Only days archived before the cutover carry the old one.
"""
import argparse
import glob
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
LEGACY_TABLE = "bus_positions_legacy_schema"
ARCHIVE_ROOT = os.environ.get("ARCHIVE_PATH", "/mnt/archive/wraith-bus-ingestor-archive")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "8"))

# The pre-2026-09-18 physical schema, exactly as it was archived under --
# SYMBOL capacities copied from a real `SHOW COLUMNS FROM bus_positions`
# taken before the migration, not guessed, so an ATTACH of an old day's
# files against this table never hits a column-shape mismatch.
LEGACY_SCHEMA_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEGACY_TABLE} (
    bus_id SYMBOL CAPACITY 65536 CACHE,
    lat DOUBLE,
    lon DOUBLE,
    bearing DOUBLE,
    timestamp TIMESTAMP,
    vehicle_id SYMBOL CAPACITY 256 CACHE,
    line SYMBOL CAPACITY 4096 CACHE,
    operator SYMBOL CAPACITY 512 CACHE,
    direction SYMBOL CAPACITY 256 CACHE,
    journey_ref VARCHAR,
    origin_aimed_dep LONG,
    published_line VARCHAR,
    origin_ref VARCHAR,
    origin_name VARCHAR,
    dest_ref VARCHAR,
    dest_name VARCHAR,
    block_ref VARCHAR,
    dest_aimed_arr LONG,
    recorded_at LONG
) TIMESTAMP(timestamp) PARTITION BY DAY
"""


def qdb_exec(query):
    url = QUESTDB + "/exec?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def is_attached(day, table=TABLE):
    resp = qdb_exec(
        f"SELECT name FROM table_partitions('{table}') "
        f"WHERE attached = true AND name = '{day}'"
    )
    return len(resp.get("dataset", [])) > 0


def table_dir_name(table=TABLE):
    """Ask QuestDB where the table lives instead of globbing for a name.
    RENAME TABLE keeps the physical directory, so after docs/adr/0006 the live
    bus_positions is bus_positions_v2~17 on disk and a `bus_positions~*` find
    matches nothing. directoryName is authoritative."""
    rows = qdb_exec(
        f"SELECT directoryName FROM tables() WHERE table_name = '{table}'"
    ).get("dataset", [])
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one table named {table}, found {rows!r}")
    return rows[0][0]


def is_legacy_partition(src):
    """True if an archived partition still has the pre-2026-09-18 layout.
    vehicle_id was dropped from bus_positions in that migration, so its column
    files (vehicle_id.d, versioned e.g. vehicle_id.d.8) exist only in
    partitions archived before it -- a physical fact, unlike the day's date."""
    return bool(glob.glob(os.path.join(src, "vehicle_id.*")))


def attach_partition(table, day, src):
    """docker cp the archived partition into `table`'s own directory inside
    the container, chown to the uid QuestDB runs as, then ATTACH it. Shared
    by both the live table (new-schema days) and the legacy shadow table
    (old-schema days) -- same mechanism, different destination table."""
    tdir = table_dir_name(table)
    dest_in_container = f"/var/lib/questdb/db/{tdir}/{day}.attachable"
    print(f"{day}: copying {src} -> {QUESTDB_CONTAINER}:{dest_in_container} ({table})")
    docker("cp", src, f"{QUESTDB_CONTAINER}:{dest_in_container}")
    docker(
        "exec", "-u", "root", QUESTDB_CONTAINER,
        "chown", "-R", "10001:10001", dest_in_container,
    )
    resp = qdb_exec(f"ALTER TABLE {table} ATTACH PARTITION LIST '{day}'")
    if "ddl" not in resp:
        raise RuntimeError(f"ATTACH failed for {day} on {table}: {resp!r}")


def restore(day):
    """Bring a day back from cold storage into live bus_positions.
    No-op if already present. A day archived under the pre-2026-09-18
    layout is rewritten through the legacy shadow table on the way in (see
    module docstring and docs/adr/0006) -- transparent to the caller either way."""
    if is_attached(day):
        print(f"{day}: already attached, nothing to do")
        return

    src = os.path.join(ARCHIVE_ROOT, TABLE, day)
    if not os.path.isdir(src):
        raise RuntimeError(f"no archived partition for {day} at {src}")

    if is_legacy_partition(src):
        print(f"{day}: archived under the pre-2026-09-18 layout -- rewriting via {LEGACY_TABLE}")
        qdb_exec(LEGACY_SCHEMA_DDL)
        attach_partition(LEGACY_TABLE, day, src)
        for _ in range(20):
            if is_attached(day, table=LEGACY_TABLE):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"{day}: ATTACH into {LEGACY_TABLE} accepted but not visible after 10s"
            )

        # Same CAST() expressions as the one-shot bulk migration (docs/adr/0006)
        # -- defined once there, mirrored here, not re-derived from scratch.
        resp = qdb_exec(
            f"INSERT INTO {TABLE} "
            f"SELECT bus_id, cast(lat AS FLOAT), cast(lon AS FLOAT), cast(bearing AS FLOAT), "
            f"timestamp, line, operator, direction, journey_ref, origin_aimed_dep, "
            f"published_line, origin_ref, origin_name, dest_ref, dest_name, block_ref, "
            f"dest_aimed_arr, recorded_at "
            f"FROM {LEGACY_TABLE} WHERE timestamp IN '{day}'"
        )
        if "ddl" not in resp and "dml" not in resp:
            raise RuntimeError(f"legacy rewrite INSERT failed for {day}: {resp!r}")
        print(f"{day}: rewritten from legacy schema and inserted into {TABLE}")
        return

    attach_partition(TABLE, day, src)
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
    # extract.py's main() does the same before its chunk loop: the pre-chunking
    # single-file name joins the same glob as the 4-hour chunks, so leaving one
    # behind double-counts the day.
    stale = os.path.join(extract.OUT, f"bus_positions_{day_obj.isoformat()}.parquet")
    if os.path.exists(stale):
        os.remove(stale)
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

    r = sub.add_parser("restore", help="ATTACH one archived day back into live QuestDB")
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
