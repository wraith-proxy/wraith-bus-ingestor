#!/usr/bin/env python
"""One-off: salvage the 63 archived days whose symbol maps were lost into Parquet.

Kept for provenance, not routine use (docs/adr/0006, docs/adr/0007). Days
archived before the 2026-09-18 schema migration hold `bus_id` and `line` as
integer keys into the old table's symbol maps, which were dropped with it, so
they can never be decoded again. What is still real is the numeric columns.
This reads those raw QuestDB column files directly and writes one Parquet file
per day: `timestamp`, `lat`, `lon`, `bearing`, plus the raw `bus_id_key` /
`line_key` integers (meaningless without the lost maps, kept so nothing that
survived is thrown away). The file's key/value metadata says so.

    uv run --no-project --with numpy --with pyarrow --with duckdb \\
        python ops/salvage_legacy_archive.py            # every old-layout day
        python ops/salvage_legacy_archive.py 2026-08-31 # one day

Run once, on 2026-09-19. The raw archive it read (`bus_positions/`) was deleted
afterwards, once every output had been independently recounted, so this can no
longer be re-run; it is kept to show how the 63 files were made. It never
modified the raw archive. A day is skipped if its Parquet file already exists.

QuestDB pre-sizes column files, so each holds a short run of zero padding after
the real rows. It is trimmed, and every assumption is asserted rather than
trusted: the zeros are contiguous at the tail, the real rows are monotonic and
inside the day, and the real row count appears in the partition's own `_txn`.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hostenv  # noqa: E402

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

US_PER_DAY = 86_400 * 1_000_000
NOTE = (
    "Numeric-only salvage. bus_id_key/line_key are raw QuestDB symbol keys whose string maps "
    "were lost when the pre-2026-09-18 bus_positions table was dropped (docs/adr/0006); they "
    "cannot be decoded."
)


def column(day_dir, name, dtype):
    (path,) = glob.glob(os.path.join(day_dir, f"{name}.d*"))  # versioned, e.g. line.d.9
    return np.fromfile(path, dtype)


def is_legacy(day_dir):
    return bool(glob.glob(os.path.join(day_dir, "vehicle_id.*")))


def salvage_day(day_dir, dest):
    """Write `dest` from the raw partition at `day_dir`; return the row count."""
    day = os.path.basename(day_dir)
    raw_ts = column(day_dir, "timestamp", "<i8")
    n = int((raw_ts > 0).sum())
    assert n > 0, f"{day}: no rows"
    assert (raw_ts[:n] > 0).all() and (raw_ts[n:] == 0).all(), f"{day}: zeros are not tail padding"
    ts = raw_ts[:n]
    assert (np.diff(ts) >= 0).all(), f"{day}: real rows are not in timestamp order"
    start = int(np.datetime64(day, "us").astype("int64"))
    assert start <= ts[0] and ts[-1] < start + US_PER_DAY, f"{day}: rows outside the day"
    txn = np.fromfile(os.path.join(day_dir, "_txn"), "<i8")
    assert n in txn, f"{day}: row count {n} not found in QuestDB's own _txn"

    cols = {"timestamp": pa.array(ts.view("datetime64[us]"), type=pa.timestamp("us", tz="UTC"))}
    for name in ("lat", "lon", "bearing"):
        values = column(day_dir, name, "<f8")[:n]
        cols[name] = pa.array(values, mask=np.isnan(values))
    for name in ("bus_id", "line", "vehicle_id"):
        keys = column(day_dir, name, "<i4")[:n]
        if (keys != -1).any():  # vehicle_id is 100% NULL: not worth a column
            cols[f"{name}_key"] = pa.array(keys, mask=keys == -1)

    table = pa.table(cols).replace_schema_metadata({
        "wraith.layout": "numeric-salvage",
        "wraith.note": NOTE,
        "wraith.source": day_dir,
        "wraith.padding_rows_trimmed": str(len(raw_ts) - n),
    })
    part = dest + ".part"
    pq.write_table(table, part, compression="zstd", compression_level=3)
    assert pq.read_metadata(part).num_rows == n
    os.replace(part, dest)
    return n


def main(days):
    archive = hostenv.get("ARCHIVE_PATH")
    RAW = os.path.join(archive, "bus_positions")
    OUT = os.path.join(archive, "bus_positions_parquet")
    os.makedirs(OUT, exist_ok=True)
    days = days or sorted(d for d in os.listdir(RAW) if is_legacy(os.path.join(RAW, d)))
    done = skipped = 0
    for day in days:
        dest = os.path.join(OUT, f"{day}.parquet")
        if os.path.exists(dest):
            skipped += 1
            continue
        n = salvage_day(os.path.join(RAW, day), dest)
        print(f"{day}: {n:,} rows, {os.path.getsize(dest) / 1e6:.1f} MB")
        done += 1
    print(f"salvaged {done} day(s), skipped {skipped} already done")


if __name__ == "__main__":
    main(sys.argv[1:])
