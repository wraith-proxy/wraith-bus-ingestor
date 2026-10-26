#!/usr/bin/env python
"""Pull a bounded window of bus_positions out of QuestDB into parquet for dbt.

QuestDB holds ~1e9 rows; dbt only needs enough to fit per-line models, so we
downsample to one row per bus per SAMPLE_MINS and keep the last EXTRACT_DAYS
UTC days. Each day is fetched once, as chunk files plus a .done marker, so a
nightly run only pulls the one day it does not have yet.
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
DAYS = int(os.environ.get("EXTRACT_DAYS", "4"))
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
# measurable difference after 30s idle) -- so the 48 queries of a full 8-day (since cut to 4)
# run still climb cumulatively, observed peaking at ~7.8GB of an 8GB cap.
# Restarting questdb between days resets that for free: nothing else reads
# this table, and the cost (~6s/restart, ghost-ingestor's writes retry through
# the gap same as any other questdb blip) is cheap next to the safety margin it
# buys. Only between days a run actually fetches, though: a day skipped for its
# .done marker sent no queries. See docs/ROADMAP.md 1.1.1.
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
    # Written beside dest (outside dbt's *.parquet glob) and renamed over it
    # only once it is known to hold rows. Writing straight to dest meant a
    # re-fetch that came back empty -- the archiver had since dropped the
    # partition -- overwrote a good chunk and then deleted the empty result.
    tmp = dest + ".tmp"
    # COPY ... TO takes no bind parameters, so quote the literals ourselves.
    src, tgt = url.replace("'", "''"), tmp.replace("'", "''")
    con.execute(
        f"COPY (SELECT * FROM read_csv('{src}', header=true, columns={columns})) "
        f"TO '{tgt}' (FORMAT PARQUET, COMPRESSION zstd)"
    )
    n = con.execute("SELECT count(*) FROM read_parquet(?)", [tmp]).fetchone()[0]
    if n == 0:
        # The ingestor was down that window. An empty file is worse than no
        # file: it still joins the glob and contributes nothing but a schema
        # to argue with, so drop it and let the gap be a gap. Only the .tmp:
        # an existing chunk at dest is never deleted here.
        os.remove(tmp)
        return None, 0
    os.replace(tmp, dest)
    return dest, n


def wal_applied():
    """(True, None) when QuestDB has applied every write it has accepted for
    bus_positions, else (False, why). bus_positions is a WAL table: a commit
    lands in the WAL first and becomes visible to queries only once the apply
    job has written it to the table, and a suspended table stops applying
    altogether. A day fetched while either was true can come back short, so
    it is marked only if, once its chunks have landed, wal_tables() shows the
    table not suspended and writerTxn caught up with sequencerTxn; otherwise
    the next run fetches it again. Any failure to ask counts as not applied."""
    q = urllib.parse.quote(
        "SELECT suspended, writerTxn, sequencerTxn FROM wal_tables() "
        "WHERE name = 'bus_positions'"
    )
    try:
        with urllib.request.urlopen(f"{QUESTDB}/exec?query={q}", timeout=30) as r:
            rows = json.load(r)["dataset"]
    except Exception as exc:
        return False, f"wal_tables() check failed: {exc}"
    if len(rows) != 1:
        return False, f"wal_tables() returned {len(rows)} rows for bus_positions"
    suspended, writer_txn, sequencer_txn = rows[0]
    if suspended:
        return False, "bus_positions is suspended in wal_tables()"
    if writer_txn != sequencer_txn:
        return False, (f"bus_positions WAL not fully applied "
                       f"(writerTxn {writer_txn}, sequencerTxn {sequencer_txn})")
    return True, None


def prune_outside_window(first_day):
    """The SSD keeps only the extract window; SATA is the archive. dbt's source
    glob (bus_positions_*.parquet) reads every file in OUT, and nothing else
    ever removed a day that had slid out of the window -- 2026-09-19's
    nightly was reading 12 days (81.9M rows) against a 4GB memory_limit tuned
    for 8. Only files whose date is before the window are touched, and a day's
    .done marker goes with its chunks."""
    removed = []
    for path in glob.glob(os.path.join(OUT, "bus_positions_*")):
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
    total, gaps, fetched = 0, [], 0
    for i in range(DAYS, 0, -1):
        day = date.today() - timedelta(days=i)
        # A UTC day is fetched once. The ingestor's ILP lines carry no
        # timestamp, so QuestDB stamps each row on arrival and nothing can be
        # stamped into a day after it ends -- and the newest day pulled here is
        # always yesterday (date.today() is UTC on this host). Re-pulling the
        # whole window every night rewrote every day but the newest from rows
        # that could not have changed, at a questdb restart apiece. The marker
        # is written only once every chunk of the day has landed, the day had
        # rows, and QuestDB had applied every write it accepted (wal_applied()).
        # A run that dies mid-day, an empty day (feed down, or the partition
        # already gone) and a lagging or suspended table all leave no marker,
        # so the next run fetches that day again. To force a re-pull, delete
        # the day's .done; deleting its chunks without it means the day is
        # never fetched again.
        done = os.path.join(OUT, f"bus_positions_{day.isoformat()}.done")
        if os.path.exists(done):
            print(f"{day} -> already extracted, skipped")
            continue
        if fetched and RESTART_QUESTDB_BETWEEN_DAYS:
            restart_questdb()
        fetched += 1
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
            print(f"{day} -> NO DATA (ingest gap), no file written, not marked done")
        else:
            print(f"{day} -> {len(chunks)} chunk(s), {day_total:,} rows")
            applied, why = wal_applied()
            if applied:
                open(done, "w").close()
            else:
                print(f"{day} -> not marked done, the next run fetches it again: {why}")
    con.close()
    if gaps:
        print(f"warning: {len(gaps)} day(s) with no data: "
              + ", ".join(f"{d} ({d:%a})" for d in gaps))
    print(f"total {total:,} rows from {fetched} day(s) fetched into {OUT}")
    # The window, not this run: a night whose only day to fetch came back empty
    # (or that found every day already marked) still has the days before it.
    # An unreachable QuestDB has already raised by here, in questdb_columns()
    # or fetch().
    first_day = date.today() - timedelta(days=DAYS)
    in_window = [
        p for p in glob.glob(os.path.join(OUT, "bus_positions_*.parquet"))
        if (m := re.match(r"bus_positions_(\d{4}-\d{2}-\d{2})", os.path.basename(p)))
        and date.fromisoformat(m.group(1)) >= first_day
    ]
    assert in_window, f"no parquet in {OUT} for any day from {first_day} on -- nothing to build"
    # Only after a successful extract: a failed or empty run must never be the
    # thing that deletes the last good data.
    pruned = prune_outside_window(first_day)
    if pruned:
        print(f"pruned {len(pruned)} file(s) outside the {DAYS}-day window "
              f"(SATA is the archive, not the SSD): {pruned[0]} .. {pruned[-1]}")


if __name__ == "__main__":
    sys.exit(main())
