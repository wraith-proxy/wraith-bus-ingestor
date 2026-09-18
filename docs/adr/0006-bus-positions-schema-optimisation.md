# ADR 0006: Optimise `bus_positions`' on-disk schema

**Status:** Executed 2026-09-18 -- `bus_positions_old` retained for burn-in
**Date:** 2026-09-18
**Related:** [docs/ROADMAP.md § 1.0.0](../ROADMAP.md), `ops/incident.py`, `dbt/models/staging/bods/_bods__sources.yml`

## Context

`questdb_data` is 44GB, almost entirely `bus_positions` (260.6M rows). This
platform serves a population of one -- the disk cost isn't about scale
headroom, it's simply bigger than the data justifies. Three real,
measured inefficiencies in the current column types:

**`vehicle_id` is a dead column.** Sampled at 100% NULL across 260.6M
rows (this repeats an earlier 31M-row sample from 2026-09-14, see
`docs/ROADMAP.md` 1.3.0 -- `bus_id` was chosen as the real identity
instead). Grepped the whole repo: zero code references it. Pure waste.

**7 `VARCHAR` columns are all wildly low-cardinality.** Sampled one day
(21.16M rows, not the full table -- `count_distinct` over all 260.6M
timed out at 60s):

| Column | Distinct/day | Repeat factor |
|---|---|---|
| `published_line` | 2,980 | ~7,100x |
| `journey_ref` | 22,076 | ~958x |
| `block_ref` | 12,193 | ~1,735x |
| `dest_name` | 12,587 | ~1,681x |
| `origin_name` | 13,003 | ~1,627x |
| `dest_ref` | 15,322 | ~1,381x |
| `origin_ref` | 15,751 | ~1,343x |

`VARCHAR` stores the full string inline per row. QuestDB's `SYMBOL` type
dictionary-encodes to a small int, referencing a table-level string pool
-- built for exactly this shape. At 1,000-7,000x repetition this is the
single biggest lever available.

**Real full-table cardinality, checked before finalizing `SYMBOL
CAPACITY`** (the one-day sample above undersold two columns --
`count_distinct` over all 260.6M rows timed out at the server's own
hard 60s statement limit as one query; split one column at a time
instead, all completed):

| Column | One-day sample | Real full-table | Verdict |
|---|---|---|---|
| `journey_ref` | 22,076 | **57,603** | sample undersold by 2.6x |
| `published_line` | 2,980 | 3,129 | close |
| `block_ref` | 12,193 | 22,638 | undersold by 1.9x |
| `origin_ref` | 15,751 | 17,601 | close |
| `dest_ref` | 15,322 | 17,479 | close |
| `origin_name` | 13,003 | 14,908 | close |
| `dest_name` | 12,587 | 14,636 | close |

`journey_ref` would have sat at 88% of its originally-proposed 65536
capacity against the real number -- bumped to 131072 in the proposed
schema below. `published_line` bumped 4096 -> 8192 for the same
reason on a thinner margin. Both changes reflected in the DDL below,
not left as a gap between what was measured and what got built.

**`lat`/`lon`/`bearing` are `DOUBLE` (8 bytes).** GPS position data has
nowhere near that precision floor (a few metres of real-world accuracy).
`FLOAT` (4 bytes) still carries ~7 significant digits -- three columns x
4 bytes x 260.6M rows is a real, easily-reasoned-about saving on its own.

## Why not 3NF

Considered and rejected. `bus_positions` is an append-only time-series
ingest at ~4,200 vehicles x 1 ping/60s -- normalising `journey_ref`,
`origin_name`/`dest_name` etc. into separate dimension tables would mean
a join on every read and a lookup-or-insert on every write, the wrong
shape for this workload. `SYMBOL` already delivers 3NF's real benefit
(each distinct string stored once, referenced by a small int) as a
storage-layer encoding instead of a schema-layer join. Staying
single-table.

## The real risk: archived partitions

`ops/incident.py restore`/`replay` (`ALTER TABLE bus_positions ATTACH
PARTITION LIST '<day>'`) requires an attached partition's on-disk schema
to match the live table's column definitions exactly -- confirmed by
reading `ops/incident.py` directly, not assumed. Every day already
archived to `/mnt/archive` (anything older than the 8-day hot window) was
written under the *current* schema. Changing `bus_positions`' column
types breaks `ATTACH` for all of that history unless it's addressed.

**Decided:** neither an upfront archive rewrite nor an accepted
regression. Data older than the 8-day hot window is rarely touched by
`restore`/`replay` in practice -- no need to pay for rewriting all of it
up front. Instead:

**Lazy, per-day rewrite, triggered only by actual use.** QuestDB has no
live "cast on attach" -- `ATTACH PARTITION` requires the on-disk file to
already match the live table's column definitions, confirmed by reading
`ops/incident.py` directly. So this isn't a SQL `CAST()` at attach time;
it's `restore()` doing the real work itself, one day at a time, only
when that day is actually requested back:

1. `restore(day)` checks the day's on-disk schema generation first
   (see contract versioning below -- this needs to be knowable, not
   inferred from file inspection each time).
2. If it's the old generation: read that single day's data out under
   the old schema (a plain `SELECT` against the still-old partition
   file, same shape as the archiver's own DETACH-and-copy read), apply
   the exact same `CAST()` expressions the main migration's one-shot
   backfill uses (see Migration plan below -- one cast expression,
   defined once, reused by both the bulk backfill and this lazy path,
   not two copies to keep in sync), write it out as a fresh
   new-generation partition file, then `ATTACH` that.
3. If it's already the new generation: straight `ATTACH`, today's
   behaviour, unchanged.

This keeps `restore`/`replay` fully functional across all history (no
capability regression), costs nothing for days that are never actually
restored (the common case), and pays the one-time rewrite cost only for
a day someone genuinely needs back -- proportional to real usage instead
of a flat upfront cost sized to the worst case.

**Data contract makes the two generations explicit, not silent -- done.**
`contracts/bus-positions.odcs.yaml` bumped `2.0.0` -> `3.0.0` (breaking
`physicalType` changes on 8 properties: `lat`/`lon`/`bearing` `DOUBLE` ->
`FLOAT`; `journey_ref`, `published_line`, `origin_ref`, `origin_name`,
`dest_ref`, `dest_name`, `block_ref` `VARCHAR` -> `SYMBOL`), a
`schemaCutoverDate` customProperty added (`2026-09-18`) as the single
source of truth `ops/incident.py restore()` reads to tell old-generation
days from new ones, and a `vehicle_id` note added confirming it was
never a documented property here to begin with -- its removal from the
physical table is not a contract break.

**`ops/incident.py` recoded to match -- done.** `restore()` now checks
`schema_cutover_date()` first. A day older than cutover attaches into a
small persistent `bus_positions_legacy_schema` shadow table (the exact
pre-2026-09-18 column layout, `SYMBOL CAPACITY` values copied from a
real pre-migration `SHOW COLUMNS`, not guessed) instead of the live
table, then gets `INSERT`ed into live `bus_positions` through the same
`CAST()` expressions the bulk backfill uses. A day on or after cutover
attaches straight into the live table, unchanged behaviour. The shadow
table is created lazily (`CREATE TABLE IF NOT EXISTS`) on first use, not
upfront -- true to "pay only for what's actually restored."

## Proposed schema

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

`vehicle_id` dropped entirely. `SYMBOL CAPACITY` values sized generously
above the sampled distinct counts (headroom for growth, not tight to
today's numbers -- a too-small capacity forces a dictionary resize, not
free). Same `DEDUP UPSERT KEYS`, same `PARTITION BY DAY` as today (see
ROADMAP 1.3.0 for why the dedup keys are what they are).

## Migration plan

Platform is fully stopped for this work (`docker compose stop`, done
2026-09-18) -- no live ingest, no live reads, no contention to design
around, same posture as the September QuestDB data-ownership migration
in ROADMAP 1.0.0.

**Disk headroom, decided:** the one-shot backfill needs the original
44GB table AND the new table to exist at once (~2x headroom), which the
SSD doesn't have free. Rather than batching the backfill to avoid the
peak, the whole `questdb_data` directory moves to `/mnt/archive` (SATA,
817GB free) for the risky part of this work -- same atomic-move pattern
already trusted in this repo (ROADMAP 1.0.0's September migration), just
with the destination swapped to slow-but-spacious disk instead of a
same-filesystem `rename()`. Confirmed via QuestDB's own docs before
committing to this over a fancier cross-instance copy: `ATTACH PARTITION`
across two separate instances is explicitly unsupported, and QuestDB's
backup/restore guidance is explicit that the only supported unit of
movement is the *whole data root*, not one table selectively -- so
moving everything, not just `bus_positions`, is the only supported path
regardless, and there was never a cheaper alternative to reach for.

The move itself needs `sudo`: it's a cross-filesystem copy (not the
same-filesystem `rename()` the September migration used), and
`questdb_data`'s contents are owned by uid 10001 (the container's
internal user) -- a plain non-root `cp`/`mv` would silently re-own
everything to whoever runs it, breaking the container's own access.
Only root can preserve ownership across that copy.

1. Move `questdb_data` to `/mnt/archive` (root, ownership-preserving copy,
   verified, then the SSD original removed only after verification).
2. Repoint `QUESTDB_DATA_PATH` (`.env`) at the SATA path, start QuestDB
   there alone (`docker compose up -d db`).
3. Bump `contracts/bus-positions.odcs.yaml` to `3.0.0` with the cutover
   date recorded -- **done**, cutover date `2026-09-18`.
4. `CREATE TABLE bus_positions_v2` as above -- capacities corrected
   against the real full-table cardinality check above, not the
   original one-day sample.
5. Backfill: `INSERT INTO bus_positions_v2 SELECT bus_id, cast(lat AS
   FLOAT), cast(lon AS FLOAT), cast(bearing AS FLOAT), timestamp, line,
   operator, direction, journey_ref, origin_aimed_dep, published_line,
   origin_ref, origin_name, dest_ref, dest_name, block_ref,
   dest_aimed_arr, recorded_at FROM bus_positions`. One-shot, not
   batched -- SATA's spare capacity removes the reason to batch, and
   the platform being fully stopped removes the reason to worry about
   contention during it.
6. **Verify before cutover, same discipline as every prior migration in
   this repo:**
   - Exact row count match, not "close enough" (`count()` both tables).
   - Spot-check aggregates: `avg(lat)`, `avg(lon)`, `min`/`max` per
     column, compare old vs new within `FLOAT`'s real precision loss
     (expect sub-metre differences, not exact equality).
   - Distinct-value counts per converted `SYMBOL` column match the
     `VARCHAR` original's distinct counts exactly (a botched cast could
     silently collapse near-duplicate strings).
   - `dbt/build.sh` `SKIP_EXTRACT=1` full run + `dbt test` (884/884)
     against data extracted from `bus_positions_v2`, same bar as
     ADR-0005's verification.
   - **On any verification failure: abort and keep the old table.**
     Decided explicitly, not left to whoever's running it -- this is a
     one-way change to the only copy of live data, so failing closed
     (drop `bus_positions_v2`, leave `bus_positions` untouched, move
     `questdb_data` back to SSD unchanged) is the only acceptable
     default. Investigating the failure happens after backing out, not
     instead of it.
7. Cutover: `RENAME TABLE bus_positions TO bus_positions_old; RENAME
   TABLE bus_positions_v2 TO bus_positions`. Keep `bus_positions_old`
   around for a real burn-in window (a week of production ingest,
   minimum) before dropping it -- this is a one-way schema change for
   the hot table, no cheap rollback after that point (the archive stays
   safely in the old generation regardless, per the lazy-rewrite
   decision above -- nothing there is destroyed by this step).
8. Stop QuestDB, move `questdb_data` back to the SSD (root,
   ownership-preserving, verified), repoint `.env` back, start the full
   platform.
9. Confirm `extract.py`'s QuestDB HTTP queries still return these
   columns as plain strings (`SYMBOL` and `VARCHAR` both serialise as
   strings over the HTTP API -- expected to be transparent to
   downstream dbt models, but confirm on a real extract before trusting
   it, not assumed from documentation).

`ops/incident.py`'s lazy per-day rewrite path (step 8 in the original
version of this plan) is **done** -- see above, not deferred to a
follow-up ticket.

## Consequences

**Measured, not estimated:** `bus_positions` went from 44GB to
**21.6GB** on disk (QuestDB's own `table_partitions().diskSize`
accounting, not `du` -- ~51% reduction). All verification gates passed
before cutover: exact row count match (260,954,698 both tables),
`avg`/`min`/`max` on `lat`/`lon`/`bearing` within sub-metre `FLOAT`
precision loss, all 7 `SYMBOL` columns' distinct-value counts matching
their `VARCHAR` originals exactly, and a full `dbt run` + `dbt test`
(884/884 pass, 0 errors) against data extracted from the new table
before it went live.

**Real, unplanned cost surfaced during execution:** the SSD does not
have room for `questdb_data`'s full round trip while `bus_positions_old`
is kept for its burn-in window -- old (44GB) + new (21.6GB) = 66GB
against only 54G free on SSD at execution time. Not anticipated in the
plan above, which assumed the round trip would just work once the
backfill itself was proven to fit on SATA. `/mnt/archive` (752G free)
comfortably holds both, so the data root is staying there through the
burn-in window rather than forcing an early drop of `bus_positions_old`
or a rushed second SSD-space-clearing pass -- a real, deliberate
trade-off (slower ingest/queries for the platform during burn-in) not
in the original plan, decided live rather than guessed at in advance.

**Real cost:** a one-way schema change to the platform's core ingest
table, plus a new code path in `ops/incident.py` (lazy per-day rewrite)
that's implemented but not yet exercised against a real archived
pre-cutover day -- first real use will be its first real test.

**Revisit if:** `restore`/`replay` on old-generation days turns out to
be needed often enough that the lazy per-day rewrite cost becomes a real
recurring burden -- at that point an upfront archive rewrite (the
rejected option 1) may be worth revisiting.
