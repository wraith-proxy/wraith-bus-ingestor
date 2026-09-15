# Roadmap

Where this is now, and what's next. Not a schedule -- just the order
things need to happen in, revised as the plan changes.

## Now: 1.0.0

A live, single-NAS deployment. Ingest, storage, transform and API all
work end to end, verified against real traffic, split out of `ghost-site`
into its own repo with full git history.

`ghost-runner` (Alpine/musl) got `duckdb` baked in (`ci-images`,
2026-09-14) -- compiled from source once at image build instead of every
CI run, since `duckdb` ships no musllinux wheel. `test.yml` syncs
`--extra dev --extra duckdb` now and the "Validate data contracts" step
is restored.

`dbt-core` is deliberately NOT baked into `ghost-runner`: one of its own
hard deps, `dbt-core-experimental-parser`, ships no musllinux wheel and
refuses to build from source at all, on any platform, without a matching
prebuilt binary -- no toolchain fixes that. Not needed there anyway --
`dbt docs generate` already runs on `ubuntu-latest` (glibc) in `pages.yml`.

`dbt/macros/_macros.yml`'s `type: SQL expression` arguments (not a real
dbt type) dropped -- the cosmetic invalid-type warning on every
`dbt docs generate` is gone. Ingestor already backs off (doubling, 15 min
ceiling, resets on success) on a dead feed; that was a stale roadmap note,
not an actual gap. Archiver runs once a day regardless, so a retry storm
was never a risk there.

**This repo now owns its data.** `maps/` (2.1G), `questdb_data/` (19G) and
`osrm_data/` (8.3G) were physically migrated out of `ghost-site/` on
2026-09-14 in a 3m33s window. Same filesystem, so each was a single atomic
`rename()` -- instant, and QuestDB's uid 10001 ownership survived untouched.
Verified by an exact row-count match over the pre-migration range
(279,722,130 rows, not merely "greater than") plus all 20 partitions still
attached. The nightly dbt cron was also still pointed at the deleted
`ghost-site/dbt` since the September split and had been silently failing;
it now points here. Two things the migration taught us, both written up in
[architecture.md](architecture.md): Compose reads a bare (non-absolute,
non-`./`) volume path as a named volume, which would silently start QuestDB
empty; and `ghost-web` caches this stack's container IPs at nginx startup,
so recreating containers needs an `nginx -s reload` or `/tiles/` 502s.

`RETENTION_DAYS` cut from 22 to 8 (7 days live + 1 day buffer) -- the SSD
was holding far more hot QuestDB history than the API actually needs.
Everything past that window still lands on `ARCHIVE_PATH` (the SATA
mount, `/mnt/archive`) via the existing DETACH-and-copy archiver, nothing
is deleted. dbt's own incremental purge window used to hardcode a second,
independent `22` in two post_hooks; both now call a single
`retention_days()` macro that reads `extract_days` from `dbt_project.yml`
(default `8`) -- one number to change, not two, and the ETA regression
trains on that same smaller window as a result. The API's `predict()` now
also has a proper fallback chain: regression, then schedule-adjusted
timetable, then the bare timetable with no delay applied, then (nothing
else available) whatever ETA this bus_id last actually had. Every bus
gets an ETA wherever one can be honestly produced, not just the ones the
regression already covered.

## 1.1.0 -- gate dbt in CI

CI still only gates tests + contracts, not a dbt build -- no job anywhere
actually runs one. `pages.yml` runs `dbt docs generate` on `ubuntu-latest`,
but that's docs, not a gated build/test. **Blocker:** `ghost-runner`
(Alpine/musl) can't run dbt at all -- `dbt-core-experimental-parser`, a
hard dep of `dbt-core`, ships no musllinux wheel and refuses to build from
source on any platform without a matching prebuilt binary, so no image fix
closes this. The fix has to be a job on `ubuntu-latest` (glibc, like
`pages.yml` already uses) that runs `dbt build` + `dbt test` and fails the
pipeline on a broken model or a failed assertion, not a job on
`ghost-runner`.

Contract versions (`bus-marts.odcs.yaml`, `bus-positions.odcs.yaml`) are
already at `2.0.0` from the September restructure -- 1.0.0 was a
commitment to no more breaking changes without a major bump, not a
version reset, and that commitment stands regardless of this gap.

## 1.1.1 -- line_stops' unbounded memory

Root cause, every fix attempted (including the ones that didn't work and
why), and the measured data behind the final design are now written up in
[ADR-0001](adr/0001-line-stops-memory-and-batching.md) and
[ADR-0002](adr/0002-line-stops-final-assembly.md) rather than repeated
here -- this section tracks only what's still open.

**Remaining:**
- No cold-cache validation -- one observed cold/warm swing (588 MiB vs
  131 MiB, identical query) means the true worst case for a genuinely
  fresh 3am run isn't fully confirmed.

The systemd timer under `ops/systemd/` replaced cron 2026-09-15 (`sudo
systemctl enable --now wraith-bus-build.timer`, crontab entry removed) --
`docs/operations.md`, `README.md` and `bus-marts.odcs.yaml`'s `refresh:`
prose updated to match. Along the way, a real questdb OOM bug (query
memory unbounded per day) and a native-memory sawtooth (Cairo's mmap'd
reader pool, not JVM heap) were found and fixed too -- see git log
2026-09-15 for the four commits.

Also done as a direct result of this incident, outside git (QuestDB's own
`server.conf` isn't tracked): QuestDB's `ram.usage.limit.bytes` was
unset (defaulting to 90% of host RAM, ~14.9GB observed -- effectively no
real ceiling on a 15GB host) and is now capped at 8GB explicitly.

## 1.2.0 -- retire the direction heuristic

`DirectionRef` started arriving in the real feed on 2026-09-12.
`int_journeys_shaped_and_directed`'s bearing/shoelace inference exists only
for historical data and feed gaps -- once enough real `DirectionRef`-backed
data has accumulated, measure how often the heuristic and the real field
actually disagree, and simplify or retire whichever side loses.

## 1.3.0 -- incident recovery: upserts, replay, drift guards

Landed 2026-09-15, prompted by that day's OOM/timer work exposing that the
system assumed BODS plus a live QuestDB table WAS the history:

- `bus_positions` now `DEDUP ENABLE UPSERT KEYS(bus_id, timestamp)`.
  `vehicle_id` was checked first and found 100% null across a 2-day, 31M-row
  sample -- a dead column, not the real per-vehicle identity, so `bus_id`
  was the only real choice.
- Native `TTL 90 DAYS` removed from `bus_positions` (was sitting silently
  underneath archiver's own 8-day DETACH-and-copy retention -- harmless
  today only by coincidence of 8 < 90, a real trap if archiver was ever
  down 82+ days or `RETENTION_DAYS` raised without noticing the table TTL
  existed). Archiver is now the sole retention mechanism.
- `ops/incident.py`: `restore <day>` (ATTACH a day back from
  `/mnt/archive`, idempotent) and `replay <start> <end>` (restore-if-needed
  + re-extract via `extract.py`'s own `fetch()` + `SKIP_EXTRACT=1
  FULL_REFRESH=1 build.sh`), one script per the "not two scripts a human
  has to sequence by hand" requirement. `restore` proven against a real
  archived day (2026-07-03, 10.6M rows, re-run correctly no-ops).
- `dbt-expectations` (`metaplane/dbt_expectations` -- the `calogica` one
  `dbt deps` warns is deprecated) plus two custom macros
  (`dbt/macros/tests/schema_drift.sql`): dbt_expectations' own
  `expect_column_values_to_be_of_type` / `expect_table_columns_to_match_set`
  turned out broken on the duckdb adapter for any table-valued-function
  source (`describe read_parquet(...)` isn't valid as a subquery) --
  confirmed by running them, not assumed, and replaced rather than
  worked around blindly. `_bods__sources.yml`'s `bus_positions` table now
  carries: source freshness (warn 2d / error 5d), not_null, per-column
  type validity, GB-bbox range warnings, compound-key
  (`timestamp, bus_id, line`) uniqueness, and a row-count floor. The
  schema-drift test was negative-tested by injecting a real extra column
  into a live parquet chunk and confirming it actually fails, not just
  parses.

**Remaining:**
- `ops/incident.py replay` is written but untested -- needs a synthetic
  "lost N days" scenario run end to end before it's trusted in a real
  incident.
- No page/alert on the freshness `warn`/dbt test `severity: warn` results
  -- they show up in `dbt test` output today, nothing routes them
  anywhere a human would see them unprompted.
