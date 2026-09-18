# ADR 0006: Optimise `bus_positions`' on-disk schema

**Status:** Proposed -- not yet implemented
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

**Data contract must make the two generations explicit, not silent.**
`contracts/bus-positions.odcs.yaml` is versioned (`version: 2.0.0`
currently) specifically so a physical schema change is a visible,
tracked event, not something a consumer discovers by accident. When this
migration executes:
- Bump to `3.0.0` (breaking `physicalType` changes on 8 properties:
  `lat`/`lon`/`bearing` `DOUBLE` -> `FLOAT`; `journey_ref`,
  `published_line`, `origin_ref`, `origin_name`, `dest_ref`, `dest_name`,
  `block_ref` `VARCHAR` -> `SYMBOL`).
- Add a `customProperties` entry recording the cutover date, so
  `restore()`'s schema-generation check has a single source of truth to
  read (`day < cutover date` = old generation, needs the lazy rewrite;
  `day >= cutover date` = new generation, straight attach) rather than
  inspecting each partition file's own column types at runtime.
- Note in `limitations` that `vehicle_id` never existed in the contract
  to begin with (confirmed -- it isn't a documented property today
  either), so dropping it from the physical table is not a contract
  break, just removing dead weight the contract never promised.

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
    journey_ref  SYMBOL CAPACITY 65536 CACHE,
    origin_aimed_dep LONG,
    published_line   SYMBOL CAPACITY 4096 CACHE,
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

## Migration plan (not yet executed)

Platform is fully stopped for this work (`docker compose stop`, done
2026-09-18) -- no live ingest, no live reads, no contention to design
around, same posture as the September QuestDB data-ownership migration
in ROADMAP 1.0.0.

1. Bump `contracts/bus-positions.odcs.yaml` to `3.0.0` with the cutover
   date recorded (see the lazy-rewrite decision above) -- this is what
   `restore()` will read to tell old-generation days from new ones, so
   it needs to exist before step 6 relies on it.
2. `CREATE TABLE bus_positions_v2` as above.
3. Backfill: `INSERT INTO bus_positions_v2 SELECT bus_id, cast(lat AS
   FLOAT), cast(lon AS FLOAT), cast(bearing AS FLOAT), timestamp, line,
   operator, direction, journey_ref, origin_aimed_dep, published_line,
   origin_ref, origin_name, dest_ref, dest_name, block_ref,
   dest_aimed_arr, recorded_at FROM bus_positions`. One-shot, not
   batched -- platform is stopped, no memory pressure from concurrent
   readers/writers to design around (revisit if QuestDB's own memory
   during a 260M-row single INSERT turns out to need it, measure before
   assuming).
4. **Verify before cutover, same discipline as every prior migration in
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
5. Cutover: `RENAME TABLE bus_positions TO bus_positions_old; RENAME
   TABLE bus_positions_v2 TO bus_positions`. Keep `bus_positions_old`
   around for a real burn-in window (a week of production ingest,
   minimum) before dropping it -- this is a one-way schema change for
   the hot table, no cheap rollback after that point (the archive stays
   safely in the old generation regardless, per the lazy-rewrite
   decision above -- nothing there is destroyed by this step).
6. Point `ghost-ingestor` at the (renamed) `bus_positions` -- no code
   change needed, it already writes to that fixed table name.
7. Confirm `extract.py`'s QuestDB HTTP queries still return these
   columns as plain strings (`SYMBOL` and `VARCHAR` both serialise as
   strings over the HTTP API -- expected to be transparent to
   downstream dbt models, but confirm on a real extract before trusting
   it, not assumed from documentation).
8. Implement `restore()`'s lazy per-day rewrite path (checks the
   contract's cutover date, rewrites-then-attaches an old-generation
   day) as its own change to `ops/incident.py`, verified against a real
   archived pre-cutover day before trusting it on an actual incident.

## Consequences

**Expected:** meaningful reduction of `bus_positions`' 44GB footprint --
real number not yet measured (would require executing the migration to
know, not estimated here to avoid presenting a guess as a finding).

**Real cost:** a one-way schema change to the platform's core ingest
table, plus a new code path in `ops/incident.py` (lazy per-day rewrite)
that needs its own testing against a real archived day before it can be
trusted mid-incident. Not a quick edit -- full plan above should be
followed in order, verified at each step, same bar as every other
migration this repo has done.

**Revisit if:** `restore`/`replay` on old-generation days turns out to
be needed often enough that the lazy per-day rewrite cost becomes a real
recurring burden -- at that point an upfront archive rewrite (the
rejected option 1) may be worth revisiting; or a `count_distinct` over
the full table (not just one sampled day) surfaces materially different
cardinality than projected here, in which case the `SYMBOL CAPACITY` sizing above
needs revisiting before creating the table for real.
