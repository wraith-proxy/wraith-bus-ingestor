# ADR 0006: Optimise `bus_positions`' on-disk schema

**Status:** Executed 2026-09-18. Its cost to the archive is irreversible and was judged a mistake by the owner (2026-09-19). Archive format superseded by [ADR-0007](0007-parquet-archive.md).
**Date:** 2026-09-18
**Related:** [docs/ROADMAP.md 1.10.0](../ROADMAP.md), [ADR-0007](0007-parquet-archive.md), `ops/incident.py`, `archiver/archive.py`, `contracts/bus-positions.odcs.yaml`

## Read this first

`bus_positions` was migrated to a tighter schema and shrank from 44GB to
21.6GB, verified end to end. That part worked.

It also broke something the plan said it would not: **63 archived days
(2026-07-03 to 2026-09-06) can no longer be restored** -- only their raw
`timestamp`, `lat`, `lon` and `bearing` survive; `bus_id` and `line` cannot be
decoded. The owner approved this breaking change and, on 2026-09-19, judged
that approval the wrong call. It cannot be undone: the data needed to decode
those days no longer exists anywhere. This ADR records what was decided, what
was measured, and exactly how the archive was lost, so the same mistake is not
made again.

## Context

`questdb_data` was 44GB, almost entirely `bus_positions` (260.6M rows). This
platform serves a population of one, so the disk cost was not about scale
headroom; it was bigger than the data justified. Three measured
inefficiencies in the column types:

**`vehicle_id` is a dead column.** 100% NULL across all 260.6M rows (it
repeats an earlier 31M-row sample from 2026-09-14, see `docs/ROADMAP.md`
1.3.0 -- `bus_id` was chosen as the real identity instead). Zero code
references it.

**Seven `VARCHAR` columns are wildly low-cardinality.** `VARCHAR` stores the
full string inline per row; QuestDB's `SYMBOL` dictionary-encodes to a small
integer. Real full-table cardinality, measured one column at a time because
`count_distinct` over all 260.6M rows as a single query hit the server's 60s
statement limit (a one-day sample undersold two columns):

| Column | One-day sample | Real full-table |
|---|---|---|
| `journey_ref` | 22,076 | **57,603** |
| `block_ref` | 12,193 | 22,638 |
| `origin_ref` | 15,751 | 17,601 |
| `dest_ref` | 15,322 | 17,479 |
| `origin_name` | 13,003 | 14,908 |
| `dest_name` | 12,587 | 14,636 |
| `published_line` | 2,980 | 3,129 |

`journey_ref` would have sat at 88% of a 65,536 `SYMBOL` capacity, so
capacities were sized above the real numbers, not the sample.

**`lat`/`lon`/`bearing` are `DOUBLE` (8 bytes).** GPS accuracy is metres;
`FLOAT` (~7 significant digits) is far past that floor.

## Why not 3NF

Rejected. `bus_positions` is an append-only time-series ingest; normalising
`journey_ref`, `origin_name` and friends into dimension tables would mean a
join on every read and a lookup-or-insert on every write. `SYMBOL` already
delivers 3NF's storage benefit as an encoding rather than a join.

## Decision

Create `bus_positions_v2` with the schema below, backfill it, verify, then
swap it in with `RENAME TABLE`. `vehicle_id` is dropped.

```sql
CREATE TABLE bus_positions_v2 (
    bus_id       SYMBOL CAPACITY 65536 CACHE,
    lat          FLOAT,
    lon          FLOAT,
    bearing      FLOAT,
    timestamp    TIMESTAMP,
    line         SYMBOL CAPACITY 4096 CACHE,
    operator     SYMBOL CAPACITY 512 CACHE,
    direction    SYMBOL CAPACITY 256 CACHE,
    journey_ref  SYMBOL CAPACITY 131072 CACHE,
    origin_aimed_dep LONG,
    published_line   SYMBOL CAPACITY 8192 CACHE,
    origin_ref       SYMBOL CAPACITY 32768 CACHE,
    origin_name      SYMBOL CAPACITY 32768 CACHE,
    dest_ref         SYMBOL CAPACITY 32768 CACHE,
    dest_name        SYMBOL CAPACITY 32768 CACHE,
    block_ref        SYMBOL CAPACITY 32768 CACHE,
    dest_aimed_arr   LONG,
    recorded_at      LONG
) TIMESTAMP(timestamp) PARTITION BY DAY
  DEDUP UPSERT KEYS(bus_id, timestamp);
```

The data contract (`bus-positions.odcs.yaml`) went `2.0.0` -> `3.0.0`, a
breaking change to eight `physicalType`s. `vehicle_id` was never a documented
property, so dropping it is not a contract break.

## What was executed

The backfill needs the 44GB original and the new table at once, which the SSD
had no room for, so the whole `questdb_data` directory was copied to
`/mnt/<archive>` (SATA) and QuestDB ran from there for the migration. The whole
data root moves, not one table: QuestDB's backup guidance offers no
supported way to move a single table between instances, and `ATTACH
PARTITION` between tables or instances is unsupported. The copy needed `sudo`
(cross-filesystem, and the contents are owned by the container's uid 10001).

Verified before cutover, all passing:
- exact row count match, 260,954,698 in both tables;
- `avg`/`min`/`max` of `lat`/`lon`/`bearing` within `FLOAT`'s sub-metre loss;
- distinct-value counts of all seven `SYMBOL` columns equal to their `VARCHAR`
  originals;
- a full `dbt run` (11 + 86 models) and `dbt test` (884/884) against data
  extracted from the new table.

Then `RENAME TABLE bus_positions TO bus_positions_old; RENAME TABLE
bus_positions_v2 TO bus_positions`. A verification failure would have aborted
the migration and kept the old table.

On 2026-09-19 the owner chose to drop `bus_positions_old` immediately rather
than run the planned week-long burn-in, and to move the data root back to the
SSD (an `rsync --delete` over the SSD's original `questdb_data`). The SSD had
been the constraint: old (44GB) plus new (21.6GB) did not fit alongside its
free space. The platform came back healthy with the ingestor writing the new
schema.

## Results

`bus_positions` went from 44GB to **21.6GB** (QuestDB's own
`table_partitions().diskSize`, ~51%). The dbt pipeline and the ingestor work
unchanged on the new schema.

## What went wrong: the archive

**The plan asserted that archived days would stay restorable, and never
tested it.** It proposed a lazy per-day rewrite (attach an old-layout archived
day into a shadow table, `INSERT ... SELECT` with `CAST()` into the live
table), and stated that "the archive stays safely in the old generation
regardless -- nothing there is destroyed." No real old-layout day was
restored before `bus_positions_old` was dropped.

The first real attempt, on 2026-08-31, failed at `ATTACH` with QuestDB's own
message: `Detached partition metadata [table_id] is not compatible with
current table metadata`. Two facts make the design impossible, both checked
directly:

1. **A partition re-attaches only into the table it was detached from.** Its
   `_meta` records the table id. QuestDB's docs say moving partitions between
   tables is unsupported; that line was read while planning and under-weighted.
2. **An archived partition is not self-contained.** `bus_id.d` and `line.d.9`
   are 4-byte integer keys into the source table's symbol maps
   (`bus_id.c/.k/.o/.v`), and none of those files are in the archive. The
   whole of `2026-08-31` is `bearing.d bus_id.d lat.d line.d.9 lon.d
   timestamp.d vehicle_id.d.8` plus `_meta _cv _txn`.

The symbol maps lived in the old table's directory (`bus_positions~8`). Two
steps removed the last copies: `DROP TABLE bus_positions_old`, and the
`rsync --delete` over the SSD original, which still held `bus_positions~8`.
The SATA working copy had already been through the drop and was later
deleted. A search of the SSD and SATA found no survivor.

`bus_positions_old` would probably have kept those days restorable -- it
retains the original table id and symbol maps, so attaching into it is the
same-table case. That was never tested. The burn-in was therefore doing more
than the rollback safety the plan credited it with, and that was not
understood when it was waived.

**Affected:** every archived day with a `vehicle_id.*` column file: 63 days,
`2026-07-03` to `2026-09-06`. Their raw numeric columns survive; `bus_id`,
`line` and `vehicle_id` are keys with no way to decode them. `restore()` now
detects these days and refuses with the reason instead of half-running.

**Unaffected, and proven:** days archived after the migration were detached
from the live table, so they re-attach into it and decode from its symbol
maps. On 2026-09-11, restored twice on the real system: 16,786,332 rows, the
exact archived count; real ids decoded (`A2BV-24` on line `181`, north-west
coordinates); the archive byte-identical afterwards.

## Also broken by the cutover

`RENAME TABLE` changes the SQL name but keeps the physical directory, so the
live `bus_positions` is `bus_positions_v2~17` on disk. The archiver and
`ops/incident.py` both globbed `bus_positions~*` and found nothing (the
archiver failed on it at 06:00 on 2026-09-19). Both now ask QuestDB for
`tables().directoryName`. Grepping for that pattern before cutover would have
caught it.

## Consequences

- The schema change is one-way. There is no old-schema hot table.
- 63 archived days are decodable only as raw numbers. That data was regarded
  as stale glacier, but "stale" was the owner's judgement to make with the
  loss known, not something to discover afterwards.
- A DETACH-and-copy QuestDB archive depends on the symbol maps and table id of
  its source table. That dependency applies to every day archived from
  `bus_positions_v2~17` too. It is removed for the future by ADR-0007.

## Lessons

- Prove a real restore of the thing you are about to make unrestorable
  **before** dropping the only copy of what makes it restorable.
- A safety margin's job is not always the one written on it. The burn-in was
  described as rollback safety; it was also load-bearing for the archive.
- When a vendor doc says something is unsupported, assume it fails and test the
  smallest real case before building on the workaround.
- Grep for physical names (directories, file patterns) before any rename.
