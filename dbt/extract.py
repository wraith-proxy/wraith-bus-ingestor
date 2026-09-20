#!/usr/bin/env python
"""Pull a bounded window of bus_positions out of QuestDB into parquet for dbt.

QuestDB holds ~1e9 rows; dbt only needs enough to fit per-line models, so we
downsample to one row per bus per SAMPLE_MINS and keep the last EXTRACT_DAYS
days. One parquet file per day, so a re-run only redoes the days it is asked for.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

QUESTDB = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")
DAYS = int(os.environ.get("EXTRACT_DAYS", "8"))
SAMPLE_MINS = int(os.environ.get("EXTRACT_SAMPLE_MINS", "3"))
# A whole day's SAMPLE BY in one query OOM-killed questdb itself (kernel
# oom-killer, not a graceful questdb error -- ram.usage.limit.bytes doesn't
# cover whatever this query allocates) once daily row counts climbed past
# ~8M, 2026-09-15. Chunking the query bounds each call's group-state the
# same way line_stops' per-line batching does -- see docs/ROADMAP.md 1.1.1.
# One file per chunk; dbt's source glob (bus_positions_*.parquet,
# union_by_name=true) already unions any number of files per day for free.
CHUNK_HOURS = int(os.environ.get("EXTRACT_CHUNK_HOURS", "4"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Chunking bounds each query's own group-state, but questdb's table-reader
# pool doesn't hand its mmap'd segments back between queries fired this close
# together (checked: lowering cairo.inactive.reader.ttl to 1s made no
# measurable difference after 30s idle) -- so the 48 queries/run still climb
# cumulatively, observed peaking at ~7.8GB of an 8GB cap. Restarting questdb
# between days resets that for free: nothing else reads this table, and the
# cost (~6s/restart, ghost-ingestor's writes retry through the gap same as
# any other questdb blip) is cheap next to the safety margin it buys. See
# docs/ROADMAP.md 1.1.1.
QUESTDB_CONTAINER = os.environ.get("QUESTDB_CONTAINER", "questdb")
RESTART_QUESTDB_BETWEEN_DAYS = os.environ.get("EXTRACT_RESTART_QUESTDB", "1") == "1"


def restart_questdb():
    subprocess.run(["docker", "restart", QUESTDB_CONTAINER], check=True,
                    capture_output=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{QUESTDB}/exec?query=select+1", timeout=2):
                return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{QUESTDB_CONTAINER} did not come back healthy within 30s of restart")


# Columns we take verbatim, and columns we take the last value of within a bucket.
#
# operator is a KEY, not a last() value, and that is deliberate (2026-09-20).
# QuestDB's SAMPLE BY groups by every non-aggregated column, so what is listed
# here IS the bucket's identity. With operator missing, two vehicles of
# DIFFERENT operators that happen to share a bus_id AND a line number in the
# same 3-minute bucket collapsed into one row (last() won) -- 28,927 rows lost
# over five days (0.052%), every one a cross-operator collision, and line '1' is
# very common. Vehicle refs are minted by each operator, so a vehicle is
# (operator, bus_id), never bus_id alone; see int_pings_segmented_to_journeys.
#
# recorded_at is the feed's own per-record observation time (epoch millis). It is
# carried ONLY so the segmentation model can order the two rows a vehicle
# produces when it is on two lines inside one bucket (196,748 such buckets in
# five days, recorded_at differs in all of them). It is often stale (a dead
# record keeps its original RecordedAtTime), so it is a tiebreak, never a time
# to trust: consumers of freshness use it, see contracts/bus-positions.odcs.yaml.
KEYS = ["timestamp", "bus_id", "operator", "line"]
WANTED = [
    "lat", "lon", "bearing", "direction", "journey_ref",
    "origin_ref", "origin_name", "dest_ref", "dest_name",
    "origin_aimed_dep", "dest_aimed_arr", "recorded_at",
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
    "origin_aimed_dep": "BIGINT", "dest_aimed_arr": "BIGINT", "recorded_at": "BIGINT",
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


def fetch(con, day, hour_start, hour_end, select, columns):
    end_day = day + timedelta(days=1) if hour_end == 24 else day
    start_ts = f"{day.isoformat()}T{hour_start:02d}:00:00.000000Z"
    end_ts = f"{end_day.isoformat()}T{hour_end % 24:02d}:00:00.000000Z"
    query = (
        f"SELECT {select} FROM bus_positions "
        f"WHERE timestamp >= '{start_ts}' AND timestamp < '{end_ts}' "
        f"SAMPLE BY {SAMPLE_MINS}m"
    )
    url = f"{QUESTDB}/exp?" + urllib.parse.urlencode({"query": query})
    dest = os.path.join(OUT, f"bus_positions_{day.isoformat()}_{hour_start:02d}.parquet")
    # COPY ... TO takes no bind parameters, so quote the literals ourselves.
    src, tgt = url.replace("'", "''"), dest.replace("'", "''")
    con.execute(
        f"COPY (SELECT * FROM read_csv('{src}', header=true, columns={columns})) "
        f"TO '{tgt}' (FORMAT PARQUET, COMPRESSION zstd)"
    )
    n = con.execute("SELECT count(*) FROM read_parquet(?)", [dest]).fetchone()[0]
    if n == 0:
        # The ingestor was down that window. An empty file is worse than no
        # file: it still joins the glob and contributes nothing but a schema
        # to argue with, so drop it and let the gap be a gap.
        os.remove(dest)
        return None, 0
    return dest, n


def prune_outside_window(first_day):
    """The SSD keeps only the extract window; SATA is the archive. dbt's source
    glob (bus_positions_*.parquet) reads every file in OUT, and nothing else
    ever removed a day that had slid out of the window -- 2026-09-19's
    nightly was reading 12 days (81.9M rows) against a 4GB memory_limit tuned
    for 8. Only files whose date is before the window are touched."""
    removed = []
    for path in glob.glob(os.path.join(OUT, "bus_positions_*.parquet")):
        m = re.match(r"bus_positions_(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        if m and date.fromisoformat(m.group(1)) < first_day:
            os.remove(path)
            removed.append(os.path.basename(path))
    return sorted(removed)


def main():
    import duckdb

    os.makedirs(OUT, exist_ok=True)
    select, columns = build_select(questdb_columns())
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    total, gaps = 0, []
    for day_num, i in enumerate(range(DAYS, 0, -1)):
        if day_num > 0 and RESTART_QUESTDB_BETWEEN_DAYS:
            restart_questdb()
        day = date.today() - timedelta(days=i)
        # Stale whole-day file from before chunking existed -- if left in
        # place, it joins the same glob as this day's new chunk files and
        # double-counts every row.
        stale = os.path.join(OUT, f"bus_positions_{day.isoformat()}.parquet")
        if os.path.exists(stale):
            os.remove(stale)
        day_total, chunks = 0, []
        for hour_start in range(0, 24, CHUNK_HOURS):
            dest, n = fetch(con, day, hour_start, hour_start + CHUNK_HOURS, select, columns)
            day_total += n
            if dest is not None:
                chunks.append(os.path.basename(dest))
        total += day_total
        if not chunks:
            gaps.append(day)
            print(f"{day} -> NO DATA (ingest gap), no file written")
        else:
            print(f"{day} -> {len(chunks)} chunk(s), {day_total:,} rows")
    con.close()
    if gaps:
        print(f"warning: {len(gaps)} day(s) with no data: "
              + ", ".join(f"{d} ({d:%a})" for d in gaps))
    print(f"total {total:,} rows in {OUT}")
    assert total > 0, "extract produced no rows -- is QuestDB reachable?"
    # Only after a successful extract: a failed or empty run must never be the
    # thing that deletes the last good data.
    pruned = prune_outside_window(date.today() - timedelta(days=DAYS))
    if pruned:
        print(f"pruned {len(pruned)} file(s) outside the {DAYS}-day window "
              f"(SATA is the archive, not the SSD): {pruned[0]} .. {pruned[-1]}")


if __name__ == "__main__":
    sys.exit(main())
