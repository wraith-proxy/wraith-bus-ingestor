#!/usr/bin/env python3
"""Write a small, synthetic bus_positions parquet file for the Learn dbt sandbox.

Not real data -- there is none to ship, and rightly so; live bus positions
belong to QuestDB, not to git. This script invents a handful of journeys with
the same column shape extract.py produces, so every real model in this project
(the segmentation, the ETA regression) has enough to do something real with.

Why a script and not a committed data file: the freshness source test wants
the newest row to be recent (`error_after: 5 days`), so the timestamps have to
move with the calendar. JOURNEYS below is the actual "fixture data" -- short,
readable, and the only part a maintainer should need to touch. Change the
coordinates, the pings-per-journey or the calendar maths and read the
comments; do not hand-edit the parquet output.

Run directly to see it self-check:  python3 generate_sandbox_positions.py
Used by ops/dbt_sandbox.sh, which passes the sandbox's data/ directory; the
anchor date defaults to yesterday, so a build run the same day the sandbox was
made still has a fresh source.
"""
import argparse
from datetime import UTC, datetime, timedelta

try:
    import duckdb
except ImportError:  # pragma: no cover -- the sandbox script checks for this first
    duckdb = None

PING_SECONDS = 180   # extract.py's own sample bucket; every model assumes 3 minutes
LAYOVER_SECONDS = 12 * 60   # gap between two journeys sharing a day; must stay
                            # under the 20-minute coverage-edge window in
                            # int_journeys_shaped_and_directed.sql, or the FIRST
                            # of the two looks like a truncated journey and gets
                            # silently dropped.
TRAIL_PINGS = 15     # stationary pings after the day's LAST journey, so ITS end
                      # is not mistaken for the moment data collection stopped

# Each entry is one real, keepable journey: (operator, line, bus_id,
# start (lat, lon), end (lat, lon), n_pings, origin/dest names+codes). ~40
# pings at 3-minute spacing is around two hours and, at these coordinates, about
# 20 km/h -- comfortably inside every plausibility filter in
# int_journeys_shaped_and_directed.sql (n_pings>=5, path_km>=0.5,
# 240s<=duration<=5h, 1<=avg speed<=90 km/h). Nobody sets `direction` here, on
# purpose, the same way the real feed doesn't for every journey -- these rows
# surface as direction = NULL, same as a real untagged journey would.
JOURNEYS = [
    ("DEMO", "1", "BUS001", (53.262, -2.628), (53.102, -2.288), 40,
     "99000001", "Duckville Interchange", "99000023", "Sandopolis Square"),
    ("DEMO", "1", "BUS002", (53.102, -2.288), (53.262, -2.628), 40,
     "99000023", "Sandopolis Square", "99000001", "Duckville Interchange"),
    ("DEM2", "1", "BUS101", (53.262, -2.628), (53.102, -2.288), 38,
     "99000001", "Duckville Interchange", "99000023", "Sandopolis Square"),
    ("DEMO", "2", "BUS003", (53.360, -2.480), (53.610, -2.230), 36,
     "99000037", "Mapleford Green", "99000046", "Bridgeton Cross"),
    ("DEMO", "2", "BUS003", (53.610, -2.230), (53.360, -2.480), 36,
     "99000046", "Bridgeton Cross", "99000037", "Mapleford Green"),
]

DAY_START_HOUR = 9   # local wall-clock hour the first journey of a day departs

# One real production check does not fit any small fixture at all: the source
# expects at least 500,000 rows (models/staging/bods/_bods__sources.yml), a
# floor sized for "did a whole-GB extract produce basically nothing", not for
# a teaching sandbox. ops/dbt_sandbox.sh patches that one number down, in its
# OWN copy of the project only -- see the comment there. Nothing here works
# around it; it is a genuinely different, smaller-scale environment.


def _positions(start, end, n):
    (lat0, lon0), (lat1, lon1) = start, end
    return [(lat0 + (lat1 - lat0) * i / (n - 1), lon0 + (lon1 - lon0) * i / (n - 1))
            for i in range(n)]


def build_rows(anchor_date):
    """One row per ping, as a list of dicts, across three days ending on
    anchor_date (inclusive), two journeys a day, scheduled back-to-back with a
    short layover so the day's data stays one continuous block; the day's
    later journey gets a stationary tail so its own end is not mistaken for
    where data collection stopped (see TRAIL_PINGS)."""
    days = [anchor_date - timedelta(days=2), anchor_date - timedelta(days=1), anchor_date]
    rows = []
    for day_i, day in enumerate(days):
        todays = [JOURNEYS[(2 * day_i) % len(JOURNEYS)], JOURNEYS[(2 * day_i + 1) % len(JOURNEYS)]]
        t = datetime(day.year, day.month, day.day, DAY_START_HOUR, 0, tzinfo=UTC)
        last_end = last_bus = last_lat = last_lon = op = line = None
        for op, line, bus_id, start, end, n, o_ref, o_name, d_ref, d_name in todays:
            aimed_dep = int(t.timestamp() * 1000)
            aimed_arr = int((t + timedelta(seconds=(n - 1) * PING_SECONDS)).timestamp() * 1000)
            journey_ref = f"SANDBOX-{op}-{line}-{day.isoformat()}-{t.hour:02d}{t.minute:02d}"
            for i, (lat, lon) in enumerate(_positions(start, end, n)):
                ts = t + timedelta(seconds=i * PING_SECONDS)
                rows.append(dict(
                    timestamp=ts, bus_id=bus_id, operator=op, line=line,
                    lat=round(lat, 6), lon=round(lon, 6), bearing=None, direction=None,
                    journey_ref=journey_ref, origin_ref=o_ref, origin_name=o_name,
                    dest_ref=d_ref, dest_name=d_name,
                    origin_aimed_dep=aimed_dep, dest_aimed_arr=aimed_arr,
                    recorded_at=int(ts.timestamp() * 1000),
                ))
            last_end, last_bus, last_lat, last_lon = ts, bus_id, end[0], end[1]
            t = last_end + timedelta(seconds=LAYOVER_SECONDS)
        for i in range(1, TRAIL_PINGS + 1):
            ts = last_end + timedelta(seconds=i * PING_SECONDS)
            rows.append(dict(
                timestamp=ts, bus_id=last_bus, operator=op, line=line,
                lat=last_lat, lon=last_lon, bearing=None, direction=None,
                journey_ref=None, origin_ref=None, origin_name=None,
                dest_ref=None, dest_name=None,
                origin_aimed_dep=None, dest_aimed_arr=None,
                recorded_at=int(ts.timestamp() * 1000),
            ))
    return rows, days[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", nargs="?", default="bus_positions_sandbox.parquet",
                     help="output parquet path")
    ap.add_argument("--anchor", default=None,
                     help="YYYY-MM-DD for the last day of data (default: yesterday, UTC)")
    args = ap.parse_args()
    anchor = (datetime.strptime(args.anchor, "%Y-%m-%d").date() if args.anchor
              else (datetime.now(UTC) - timedelta(days=1)).date())
    if duckdb is None:
        raise SystemExit("duckdb is required (use the project's .venv)")

    rows, _first_day = build_rows(anchor)
    con = duckdb.connect()
    con.execute("create table t as select * from (values " + ",".join(
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)" for _ in rows) + ") "
        "t(timestamp, bus_id, line, lat, lon, bearing, direction, operator, "
        "journey_ref, origin_ref, origin_name, dest_ref, dest_name, "
        "origin_aimed_dep, dest_aimed_arr)",
        [v for r in rows for v in (
            r["timestamp"], r["bus_id"], r["line"], r["lat"], r["lon"], r["bearing"],
            r["direction"], r["operator"], r["journey_ref"], r["origin_ref"],
            r["origin_name"], r["dest_ref"], r["dest_name"], r["origin_aimed_dep"],
            r["dest_aimed_arr"])])
    con.execute("alter table t add column recorded_at bigint")
    con.execute("update t set recorded_at = epoch_ms(timestamp)")
    n_real = con.execute("select count(*) from t").fetchone()[0]

    con.execute("alter table t alter bearing type double")
    con.execute("alter table t alter direction type varchar")
    con.execute(f"copy t to '{args.out}' (format parquet)")
    n_total = n_real
    print(f"wrote {args.out}: {n_total} pings, anchored on {anchor}")


if __name__ == "__main__":
    main()
