# ADR 0007: Archive to Parquet, not raw QuestDB partitions

**Status:** Accepted and implemented 2026-09-19
**Date:** 2026-09-19
**Related:** [ADR-0006](0006-bus-positions-schema-optimisation.md), `archiver/archive.py`, `ops/incident.py`, `ops/salvage_legacy_archive.py`, `ops/grafana/provisioning/alerting/rules.yaml`

## Context

Until now the archive was DETACHed QuestDB partition directories copied to the
SATA drive. ADR-0006 showed that is a fragile format: a partition's SYMBOL
columns are integer keys into the source table's symbol maps, which are not
archived with it, and QuestDB re-attaches a partition only into the table it
came from. Drop or recreate `bus_positions` and the whole archive is stranded;
63 days were lost that way.

The owner's decision (2026-09-19): move the archive to Parquet, and archive
every future day as Parquet.

## Decision

The archiver exports each day older than `RETENTION_DAYS` to
`${ARCHIVE_PATH}/bus_positions_parquet/<day>.parquet` using QuestDB's own
native export (`/exp?fmt=parquet`), then drops the live partition -- but only
after the file has been proven:

1. stream the export to `<day>.parquet.part` (zstd, level 3);
2. read it back with DuckDB: its row count must equal QuestDB's `count()` for
   that day, and its first and last timestamps must fall inside the day;
3. rename into place, `chown` to the archive owner, then `ALTER TABLE ... DROP
   PARTITION`.

A failed verification discards the file and leaves the live day untouched. A
day whose file already exists (a restored day coming back) is not re-exported:
if the file matches the live rows the live copy is dropped, otherwise the day
is skipped and reported, and the run carries on. The archiver no longer
mounts QuestDB's data directory; it works over QuestDB's HTTP API only.

`ops/incident.py restore` reads the Parquet file, copies it into QuestDB's
`import` directory and `INSERT`s it through `read_parquet()`, checking the row
count against the file. The archive can also be queried in place with DuckDB.

Each archiver run writes a row to `archiver_runs` (`ok` / `attention` /
`error`). Two Grafana rules read it and reach the phone through ntfy: no `ok`
run in 36h, and the latest run not `ok`.

## Measured, on the real system (2026-09-19)

Codec, on a full 34,149,781-row day (2026-09-16, ~3.0GB as a QuestDB
partition):

| Codec | Size | Time |
|---|---|---|
| QuestDB default (lz4) | 1.22GB | 28s |
| zstd level 3 | **0.69GB** | 25s |
| zstd level 9 | 0.67GB | 58s |

Level 3 is chosen: level 9 saves 3.6% more for 2.3x the time. The export
streams: QuestDB peaked at ~4GB of its 8GB, the client at 11MB.

Conversion of the five days archived after the schema migration
(2026-09-07 to 09-11): raw QuestDB partitions of 1.25-1.49GB became Parquet
files of 77-88MB (mostly-NULL schedule columns compress unusually well, so this
is not the ratio to expect from a full-column day).

Round trip, on 2026-09-11: restoring the file gave back exactly 16,786,332 rows
and matched the file on distinct buses (6,684), distinct lines (1,533) and the
lat/lon/bearing sums. The archiver then hit "already archived (16,786,332 rows
verified)" and dropped the live copy.

Alert delivery: a hand-inserted `error` row in `archiver_runs` took the
"archiver needs attention" rule to pending at 07:35 and firing at 07:40 (five
minute evaluation, five minute `for:`); ntfy counted one message published to one
subscriber, and the alert arrived on the owner's phone. The rule cleared at the
next evaluation once an `ok` row was written. Not confirmed: that a separate
"resolved" message reached the phone.

## The 63 numeric-only days

The days archived before the migration (2026-07-03 to 2026-09-06) cannot be
decoded (ADR-0006). `ops/salvage_legacy_archive.py` reads their raw column files
and writes `timestamp`, `lat`, `lon`, `bearing` plus the raw `bus_id_key` /
`line_key` integers, with `wraith.layout = numeric-salvage` in the file's
metadata. QuestDB pre-sizes column files, leaving 55-305 zero rows of padding
per day; the script trims it and asserts, rather than assumes, that the zeros
are contiguous at the tail, the real rows are ordered and inside the day, and
the real row count appears in the partition's own `_txn`. `restore` refuses
these days (there is no `bus_id` to load). Read them in place with DuckDB and
`union_by_name = true`.

## What went wrong while building it

Both found by running it against the real system, not by the unit tests:

- **`pytz` missing in the archiver image.** Reading a Parquet `TIMESTAMPTZ`
  back into a Python datetime needs it and the slim image has none. The first
  real run crashed. It failed closed (nothing dropped, the half-checked file
  was never promoted, and the error was recorded in `archiver_runs`); the fix is
  to compare dates as text. The unit tests could not have caught it: they run
  in a venv that has `pytz`.
- Two smaller bugs in `ops/incident.py` and the tests, fixed the same day.

## Consequences

- The archive no longer depends on `bus_positions`, its table id or its symbol
  maps: it can be dropped and recreated without stranding history.
- Parquet is smaller (0.69GB against ~3.0GB for a full-column day) and readable
  in place, so a look at old data no longer needs a restore.
- A restore is now an `INSERT`, not an `ATTACH`: slower, but it works into any
  table, and the table's dedup keys apply.
- A file is one day at the schema of that day. Columns added later read as NULL
  through `union_by_name`; `ops/incident.py`'s `COLUMNS` list must be kept in
  step with the table if it ever changes.
- The archiver depends on QuestDB's `/exp?fmt=parquet` and `read_parquet()`.
  The image is `questdb/questdb:latest`, so a future release that changes either
  would surface as an `error` run, not silent corruption: nothing is dropped
  unverified.
- The 68 raw partition directories (46.7GB) were **deleted on 2026-09-19 at the
  owner's instruction**, after verification: all 68 days had a non-empty Parquet
  file; an independent recount of the 63 salvaged days (898,814,870 rows)
  against their raw column files matched on row count, timestamp range and
  latitude sum, and the 5 converted days matched their original counts; and the
  raw archive was byte-identical throughout. A manifest of what was removed is
  kept on the archive drive (`raw_partitions_deleted_2026-09-19.txt`). This is
  irreversible, and `ops/salvage_legacy_archive.py` can no longer be re-run: its
  source is gone. It is kept as provenance for how the 63 files were made.
- The whole archive is now 68 files, 10GB (the raw copy was 46.7GB).

## Lessons

- A conversion is not done until it has been restored from and compared: the
  round-trip fingerprint is what proves the format, not the export succeeding.
- Test in the real container. A dependency missing from a slim image does not
  show in a venv that has it.
- Keep the old format untouched until the new one has been restored from and
  independently recounted. Only then delete it.
