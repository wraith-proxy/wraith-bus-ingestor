# ADR 0010: ETA from daily statistics, and a 3-day hot window

**Status:** Accepted 2026-10-25, not yet implemented. Lands in steps (PR1, its seeding nightly, PR2, the compaction, then the `.env` change; see "Rollout"); record each here as it happens.
**Date:** 2026-10-25
**Related:** [ADR-0003](0003-replay-verified-by-construction.md), [ADR-0006](0006-bus-positions-schema-optimisation.md), [ADR-0007](0007-parquet-archive.md), [ADR-0009](0009-fast-nightly-build.md), `dbt/models/intermediate/int_eta_daily_stats.sql`, `dbt/models/intermediate/int_journey_delays.sql`, `dbt/models/marts/eta_models.sql`, `dbt/models/marts/line_punctuality.sql`, `dbt/tests/assert_eta_stats_match_raw_fit.sql`, `dbt/extract.py`, `dbt/build.sh`, `ops/test_extract_markers.py`, `ops/incident.py`, `ops/compact_build_db.sh` (PR2), `contracts/bus-marts.odcs.yaml`

## Context

The SSD is the scarce resource on this host. What was on it, measured on
2026-10-25:

| What | Size |
|---|---|
| QuestDB `bus_positions` | 24G: nine UTC-day partitions of 2.4-2.9G each, `RETENTION_DAYS=8` |
| `dbt/wraith_bus_ingestor_build.duckdb`, the scratch warehouse | 10.7 GB. `int_pings_segmented_to_journeys` is 7.97 GB of it: 173.5M rows, of which 104.0M are live, over nine local dates (~13M a day); the other 40% are deleted rows DuckDB never reclaims. Its `shoelace_term` column alone is 754 MB. The other tables are 1.07 GB, and 1.62 GB is free blocks |
| `dbt/data/*.parquet` | 3.1G: all eight days, pulled again from QuestDB every night in 4-hour chunks, with QuestDB restarted between days |
| `dbt/wraith_bus_ingestor.duckdb`, what the API reads | 5.8M: `eta_models`, `line_punctuality`, `localities` |

Eight days of raw pings are kept, three times over, for one reason.
`eta_models` is an ordinary least squares fit of `remaining_secs` on
`remaining_km` (`regr_slope`, `regr_intercept`, `regr_r2`, `regr_count`) at
eleven grains, refitted every night from `int_pings_measured_to_journey_end`:
one row per training ping over the whole window. `line_punctuality`, the
median and p90 delay per line, direction, day type and hour band, reads
journeys from `int_journeys_shaped_and_directed`, and those exist only for as
long as their pings do.

Neither needs the raw rows. An OLS fit needs six numbers per cell: the count,
the two means, and the centred sums of squares and products. Those add up
across days and across cells exactly (the pooled, or Chan, formulas), so a day
of pings can be reduced to one row per (operator, line, direction, day type,
hour band) and refitted later at any coarser grain. Punctuality needs one row
per journey, not one per ping.

The owner's brief: cheap, fast and correct, not an optimised data-engineering
solution. No streaming to SATA every 60 seconds; a nightly archive to SATA,
which the archiver already is (ADR-0007), is fine. The owner's decisions
(2026-10-25): eight days of ETA history, as today; QuestDB keeps three days;
drop `shoelace_term`.

## Decision

Keep a small daily history in dbt, fit the marts from it, and cut the raw
windows: QuestDB to three UTC days (`RETENTION_DAYS=3`, the hot window of the
title), dbt's extract to four (`extract_days=4`).

1. **`int_eta_daily_stats`**, a new intermediate model. One row per
   (`stats_date`, operator, line, direction, day_type, hour_band) with
   `n_obs = regr_count`, `avg_km = regr_avgx`, `avg_secs = regr_avgy`, and
   `sxx`, `sxy`, `syy` from `regr_sxx`, `regr_sxy`, `regr_syy`, all of
   `remaining_secs` on `remaining_km`. `stats_date` is the local
   (Europe/London) date the journey ended, joined from
   `int_journeys_shaped_and_directed` on `journey_key`; the other columns are
   per ping, exactly as `int_pings_measured_to_journey_end` has them, and NULLs
   stay their own groups. Incremental, `delete+insert` on `stats_date`.
    - **Rewrite set.** A run rewrites every `stats_date >= max(stats_date) - 2
      days`, that max read from the table as it stood before the run; a first
      build writes every date. It is the pattern segmentation already uses
      (ADR-0009), for three reasons. In BST the newest local date an extract
      reaches is a one-hour stub (the extract stops at 00:00 UTC, 01:00 BST);
      anchored on the newest raw date instead, one missed night would leave
      that stub standing as the whole of its date. Anchored on the table's own
      max, a missed or failed night is healed by the next one that succeeds,
      while the raw window still holds the dates. Two days back also covers a
      journey whose end moves to the next date when the next night's pings
      extend it (journeys are at most 5 h long): it moves, rather than being
      counted under both dates. And the rule reads only `{{ this }}`, never a
      `ref()` inside `is_incremental()`, so it needs no `-- depends_on:` hint
      and cannot raise dbt's `RefBadContextError`.
    - **Pruning.** A post-hook, skipped under `replay` like segmentation's,
      deletes every date older than `max(stats_date) - history_days`, which
      keeps nine local dates, the span the fit trains on today.
    - **Refresh.** `full_refresh = var('replay', false)`. A plain
      `FULL_REFRESH=1` leaves the history on its incremental path; a replay
      (`FULL_REFRESH=1 REPLAY=1`) rebuilds it from what `data/` holds, which
      is what replay means for every other model.
2. **`eta_models` pools `int_eta_daily_stats`** over every retained date and
   every finest cell in a rung:
   `N = Σ n_obs`, `MX = Σ n_obs·avg_km / N`, `MY = Σ n_obs·avg_secs / N`,
   `SXX = Σ (sxx + n_obs·(avg_km − MX)²)`,
   `SXY = Σ (sxy + n_obs·(avg_km − MX)·(avg_secs − MY))`,
   `SYY = Σ (syy + n_obs·(avg_secs − MY)²)`; then
   `slope = SXY / SXX`, `intercept = MY − slope·MX`, `n_obs = N`, and
   `r2 = least(1, SXY² / (SXX·SYY))`. The edge cases mirror DuckDB's
   `regr_*`: when `SXX = 0` there is no usable slope and intercept and r2 are
   NULL, and when `SYY = 0` r2 is 1, as `regr_r2` gives for a constant y; the
   sanity filters drop both. The cap keeps rounding from pushing a
   near-perfect fit over 1, which bus-marts' `eta_r2_in_range` gate would
   refuse to publish. The same eleven grains, Jinja loop, null guards,
   sanity filters, dedupe and output columns.
3. **`int_journey_delays`**, a new intermediate model with the same config,
   rewrite set, refresh rule and post-hook: one row per journey
   `line_punctuality` uses (`stats_date`, `journey_key`, line, direction,
   day_type, hour_band, `delay_s`), with exactly the filters it had
   (`dest_aimed_arr_ts` not null, direction not null, delay within ±7200 s),
   and `journey_key` tested unique. `line_punctuality` reads it; same columns,
   median, p90, count and `having`.
4. **A `history_days` dbt var**, 8, read through a `history_days()` macro
   beside `retention_days()`. **`extract_days` goes from 8 to 4** (the
   `EXTRACT_DAYS` default in `build.sh`, the var in `dbt_project.yml`, the
   default in `extract.py`); `retention_days()` still reads it, so
   segmentation keeps five local dates. Four, not three like QuestDB:
   segmentation's incremental run reads back to `M - 2`, where `M` is
   the newest local date the previous night left in its table, to carry
   journeys across midnight. In GMT that is the UTC day four days before the
   run, which a three-day extract would not hold. The extract's older days come
   from `data/` (decision 8), not from QuestDB.
5. **`RETENTION_DAYS` from 8 to 3** in `.env`: QuestDB keeps the UTC
   partitions of today and the two days before it.
6. **Drop `shoelace_term`** from `int_pings_segmented_to_journeys`, a leftover
   of the direction heuristic retired in `docs/ROADMAP.md` 1.2.0, with anything
   that exists only to compute it: 754 MB, 9.5% of the table.
7. **`FULL_REFRESH=1` stops deleting the build file.** It only passes
   `--full-refresh`, which rebuilds segmentation and every table model from
   `data/` and, by decision 1, leaves both history tables alone.
8. **`extract.py` writes `data/bus_positions_<day>.done`** once a day is
   complete, and skips any day that has one. QuestDB is restarted only between
   days actually fetched. Each chunk is fetched to `<chunk>.tmp` and renamed
   into place only when it holds rows, so a 0-row fetch never replaces or
   deletes a chunk already on disk. The marker is written only when the day's
   total is above 0 and QuestDB's `wal_tables()` shows `bus_positions` not
   suspended with `writerTxn` equal to `sequencerTxn` (every committed write
   applied; it is a WAL table, ADR-0006), so a day read while WAL apply lagged
   is fetched again the next night. The run's final check becomes that the
   window holds at least one parquet file, since a night with every day marked
   fetches nothing (QuestDB being down already raises).
   `prune_outside_window` removes markers outside the window as well, and
   `ops/incident.py`'s `extract_day` deletes a day's marker before it fetches
   the day again. The marker rules are tested in `ops/test_extract_markers.py`,
   under `ops/` because that is where CI's pytest collects.
9. **A one-off compaction**, `ops/compact_build_db.sh`, run by the owner;
   `--check` runs its preflight alone. It refuses between 01:30 and 05:00 UTC,
   while `wraith-bus-build.service` reports `active` or `activating` (a running
   oneshot is `activating`), while dbt, `build.sh` or `extract.py` is running,
   and unless both history tables exist with at least nine `stats_date`s, so it
   cannot run before the seeding nightly and seed the history thin. It backs
   the build file up to `/mnt/archive` (`rsync --bwlimit=100000`), copies the
   three incremental tables into a fresh DuckDB file
   (`int_pings_segmented_to_journeys` trimmed to the five dates its post-hook
   keeps at `extract_days=4`, without `shoelace_term`, and both history tables
   whole), verifies the row counts, and swaps. Table models and views are not
   copied: dbt rebuilds every one of them the next run, and a copied table
   model would leave the file one rebuild larger, since DuckDB does not give
   back the space of the copy dbt then replaces. The vault2 copy is the
   rollback.
10. **Rollout** in steps; see below.
11. **A permanent parity guard**, `dbt/tests/assert_eta_stats_match_raw_fit.sql`,
    two checks with their own inline pooling formula, not code shared with
    `eta_models`:
    - `int_eta_daily_stats` against `regr_count`, `regr_slope`,
      `regr_intercept` and `regr_r2` over the
      `int_pings_measured_to_journey_end` rows attributed to the same dates,
      per line and for the network, on `stats_date >= max(stats_date) - 2
      days`, dates every run rewrites. This guards the daily statistics and
      their date attribution.
    - `eta_models`' per-line rows (rung 10) and network row (rung 11) against
      the test's own pooling of `int_eta_daily_stats` over every date, through
      the same sanity filters. This guards `eta_models`' own pooling at those
      two grains; the nine finer rungs are not compared.

    Counts must match exactly. Slope, intercept and r2 fail when exactly one
    side is NULL, or when `abs(a - b) > 1e-6 * greatest(1, abs(a), abs(b))`.
    The floor of 1 is for intercepts near zero, where rounding is a large
    fraction of a tiny value; a formula bug moves a fit by far more than 1e-6.
    `build.sh` runs `dbt test` unselected, so a failure stops the publish.

## Measured, read-only, on the real build file (2026-10-25)

A probe at 17:28 UTC built both history models in memory from the live build
file, opened read-only (23 s, peak RSS 3.2 GB), and compared the marts:

- **ETA.** `eta_models` pooled from `int_eta_daily_stats` (334,734 rows over
  nine `stats_date`s, about 45k a day) against the published `eta_models`:
  112,319 keys on both sides, none on one side only, 0 `n_obs` mismatches.
  Largest relative differences: slope 4.1e-14, r2 2.0e-13, intercept 1.8e-10
  (at an intercept of −0.0026 s); the largest absolute intercept gap anywhere
  is 5.1e-11 s. For scale, a plain `regr_*` refit over the same table differs
  from the stored model by up to 3e-12 relative on its own: summation order.
  `sum(n_obs)` is 33,970,135, which is `count(*)` of
  `int_pings_measured_to_journey_end`, so the join to the journeys drops
  nothing.
- **Punctuality.** `line_punctuality` from `int_journey_delays` (1,436,939
  rows, about 200k a day) against the published one: 27,440 keys on both
  sides, 0 mismatches on `n_journeys`, median and p90 (exact comparison), and
  `sum(n_journeys)` 1,430,196 on both.
- **The build file**, as in "Context". The probe put the compacted file at
  about 2.8 GB: 1.69 GB of segmentation at four dates without
  `shoelace_term`, plus the other tables' 1.07 GB.

Two first designs were measured and dropped:

- **A rewrite set anchored on the newest raw date** (`max(ping_local_date) - 1
  day`). Replayed for five days of the whole fleet through the compiled SQL, in
  BST it last wrote 10-22 and 10-23 when only one hour of the following date
  had been extracted, 0.16-0.18% of training pings away from the full-day
  view, and 1 and 6 journeys then moved to the next date, so were counted
  under both. One missed BST night
  would have left almost a whole date as its one-hour stub. Hence decision 1's
  rewrite set.
- **A purely relative 1e-9 tolerance** in the guard. In an emulation of CI's
  sandbox build it failed on a correct implementation: the fixture's line `2`
  has an intercept under a millisecond, and the two sides differed by 4e-9 to
  6e-9 relative. On real data the smallest intercept is 0.0026 s. Hence the
  floor in decision 11.

## Rollout

The nightly runs whatever is checked out in this working tree at 03:00, and CI
auto-merges on green, so **the gate is the working tree, not the merge**.

1. **PR1**, with the window still eight days: decisions 1, 2, 3, 7, 8 and 11;
   `history_days` from decision 4; the yml docs and tests, the bus-marts
   contract's prose (3.1.1), the docs and this ADR. Before it sits in this
   tree at 03:00: `dbt parse`, the CI-equivalent sandbox run
   (`ops/dbt_sandbox.sh`), a two-night simulation on real data in scratch (a
   cold build on three days of `dbt/data` parquet, then one more day
   incrementally: `journey_key` unique, the guard passing, the rewrite set
   right), the guard run read-only against the real build file, and pytest.
   It lands whole, outside 01:30-05:00 UTC.
2. **The seeding nightly.** No markers exist yet, so it pulls all eight days,
   and its first build of both history tables seeds nine `stats_date`s.
   Morning check: both tables hold nine dates, `dbt_build_runs` says success,
   the guard passed, and there are eight `.done` markers.
3. **Night 2**, the first incremental night of the history: the extract
   fetches one day, with no QuestDB restart. Check the extract time in
   `build.log`.
4. **PR2**, prepared in a separate git worktree: `extract_days` from 8 to 4,
   decision 6, `ops/compact_build_db.sh` and its test, `ops/incident.py`'s
   `RETENTION_DAYS` default from 8 to 3 (it does not read `.env`), the
   bus-positions contract, and docs. It stays out of this tree until step 5:
   dropping a column from an incremental model breaks its next incremental
   run while the table still has the column, because with the default
   `on_schema_change` dbt inserts the target table's own column list, and the
   model no longer selects `shoelace_term`. The reverse is safe: PR1's model
   runs against a table without the column.
5. **Compaction** (the owner, outside 01:30-05:00 UTC): `--check`, then the
   real run. The script ships with PR2, so it runs from the PR2 worktree,
   pointed at this tree's build file and `.venv` (`COMPACT_DB`,
   `COMPACT_PY`). Then check out PR2 here, run the sandbox parse, and leave it
   for 03:00.
6. **The first nightly on PR2:** success, the build file's size, five
   segmentation dates, `data/` pruned to four days.
7. **Then the owner**, outside 00:00-05:00 UTC: `docker restart questdb`, set
   `RETENTION_DAYS=3` in `.env`, and `docker compose up -d archiver`. The first
   sweep exports five or six days to vault2 (~3.5G written); watch
   `archive.log` and `docker stats questdb`. Then `uv run poe docs-dbt`
   (outside 01:30-05:00 UTC) to drop `shoelace_term` from the generated docs.

## Consequences

- **SSD.** Expected, not yet measured: QuestDB from nine partitions to three
  or four, about 6-8.5G; `dbt/data/` from eight days to four, about 1.7G of
  today's 3.1G; the build file to about 3 GB (the probe's 2.8 GB was for four
  segmentation dates; five are kept), plus the two history tables. Together
  about 11-13G, from about 38G. Measure each after its step and amend this ADR.
- **The ETA does not change.** Pooled sufficient statistics give the
  coefficients a fit over the raw pings would, to floating-point rounding
  (measured above). The guard checks the daily statistics on the newest dates
  and `eta_models`' line and network rows every night, and a failure stops the
  publish. The fit still trains on nine local dates (`history_days`), but that
  number no longer costs SSD and can be set on its own.
- **Punctuality does not change:** the same journeys, filters and aggregates.
- **The build file is no longer a pure cache.** It holds the only copy of the
  history older than the raw window in `data/`: five of its nine local dates
  once PR2 lands. Lose the file and they are gone from dbt; a rebuild gets back
  only what `data/` still holds. There is no backup of the history, by choice:
  it regrows over eight nights, with the fallback rungs keeping ETAs sane
  meanwhile, or a replay from the vault2 Parquet archive (ADR-0007) rebuilds
  it. The compaction's vault2 copy covers that one moment, not later nights.
- **When a date reaches the history.** Every nightly rewrites each date from
  two days before the newest date the history held when it started, so in
  steady state a date is written by four nightlies and then not again. A
  missed or failed night is healed by the next one that succeeds, as long as
  the raw window still holds the dates it missed. Data that arrives later for
  an older date does not reach the history, the trade-off ADR-0009 already
  accepted for segmentation.
- **`replay`** (`ops/incident.py replay`, `SKIP_EXTRACT=1 FULL_REFRESH=1
  REPLAY=1`) rebuilds both history tables from what `data/` holds at that
  moment: the raw window plus the days replayed. History dates that are in
  neither drop out, and the history is short until enough nightlies have run.
- **`FULL_REFRESH=1`** rebuilds segmentation from `data/` and leaves the
  history alone. It no longer shrinks the file either; the compaction script is
  the one thing that does.
- **Extract.** A normal night fetches one day, yesterday, with no QuestDB
  restart. For eight days the extract was about ten minutes of each nightly
  (ADR-0009); not yet measured. A marked day is never fetched again, so the
  repair a full re-pull gave a short day the next night is gone; the WAL check
  in decision 8 stands in for it, and deleting a day's `.done` file forces a
  new fetch. Once `RETENTION_DAYS` is 3, a day the extract misses is retried
  for one or two nights while QuestDB still holds it, against about seven
  today; nothing alerts on it beyond `extract.py`'s `NO DATA` line.
- **QuestDB memory.** The extract no longer restarts QuestDB on a normal
  night, and those restarts were also keeping its memory down (5.7 of 8 GiB
  with nine partitions). The owner watches `docker stats questdb` daily for a
  week and restarts it once right before the archiver recreate (Rollout step
  7). A nightly restart comes back only if memory climbs.
- **The `replay` boundary.** `retention_days()` becomes 4 and `RETENTION_DAYS`
  3, so the post-hook boundary and the archive boundary no longer coincide,
  which ADR-0003 names as a reason to revisit it. The post-hooks are skipped
  under replay either way, which was ADR-0003's fix.

## Rollback, step by step

- **PR1, before PR2 lands:** `git revert` it, outside 01:30-05:00 UTC. The
  raw window is still eight days, so `eta_models` and `line_punctuality` go
  straight back to fitting from it. The two history tables stay in the build
  file, unread: drop them by hand or leave them. The `.done` markers become
  inert (`rm dbt/data/*.done`), and `FULL_REFRESH=1` deletes the file again.
- **PR2, before the `.env` change:** `git revert` it. `extract.py` fetches the
  pruned days again while QuestDB still holds them; run `FULL_REFRESH=1` if
  segmentation should cover them too. The ETA and punctuality need neither:
  they read the history. Reverting decision 6 puts `shoelace_term` back in the
  model, but dbt does not add a new column to an existing incremental table,
  so it stays absent until a `FULL_REFRESH=1`. Nothing reads it.
- **PR2, after the `.env` change:** the same, except that QuestDB no longer
  has the older days; `ops/incident.py restore` brings them back from vault2
  one day at a time.
- **Compaction:** with pre-PR2 code checked out (the backup still has
  `shoelace_term`, which PR2's model cannot insert into), copy the vault2
  backup back over `dbt/wraith_bus_ingestor_build.duckdb`, outside 01:30-05:00
  UTC, with nothing holding the file open. The history comes back as it was at
  the backup; the next nightly rewrites the nights since from the raw window,
  and any it no longer holds are lost.
- **`RETENTION_DAYS`:** set it back to 8 and recreate the archiver container.
  Days already archived stay archived; QuestDB grows back to eight days over
  the next five, or `ops/incident.py restore` brings them back sooner.

## What is not yet proven

- Two consecutive incremental nights of the real models. The probe built
  first builds only, and CI only ever does a first build; the two-night
  simulation in Rollout step 1 and night 2 are the first.
- The guard's tolerance on real data night after night. The probe did not run
  it; the read-only run in Rollout step 1 is the first.
- The memory of `dbt test` with the guard and the new yml tests, under the
  shared 6 GB pool.
- Every size and time under "Consequences".
- The compaction script against the real 10.7 GB file. Its test runs on a
  throwaway warehouse, locally only: the CI image has no `rsync`.
- QuestDB's memory without the extract's restarts, and the first sweep after
  the `.env` change exporting five or six days in a row.
