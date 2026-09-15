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

## 1.1.1 -- fix line_stops' unbounded memory, not just its ceiling

2026-09-14: a `dbt run --select line_stops` test OOM'd the whole host and
needed a physical reboot (not the clean DuckDB-level error a bumped
`memory_limit` normally produces). Root cause, confirmed by measurement:
`observations` unions the raw `int_pings_matched_to_stops` rows FOUR times
(real-operator/real-day, real-operator/`*`-day, `*`-operator/real-day,
`*`-operator/`*`-day) before `by_grain` runs an exact `median()` over
~1.5M distinct (operator, line, direction, day_type, atco_code) groups --
measured at ~22.7M rows into that aggregate on the current 8-day window.
Exact `median()` has to hold every row per group in memory until the group
closes, so this scales with total unioned volume, not with the retention
window -- it will keep getting worse as more operator-tagged data
accumulates (operator only started arriving 2026-09-12), independent of
anything else in this repo.

Tried and rejected as insufficient on its own: swapping `median()` for
`approx_quantile(secs_from_origin, 0.5)` (bounded t-digest, and this mart
already documents itself as "roughly how far along the route" -- no real
precision lost). Tested hard-capped at 12GB RAM / 24GB RAM+swap in an
isolated `docker run --memory=12g --memory-swap=24g` container (so a
repeat wouldn't take the host down again) -- it still didn't converge,
pinned at the cap with climbing disk spill and near-idle CPU, killed after
5 minutes with no progress. The union-of-4 fanout into a high-cardinality
exact aggregate is the actual cost; changing which aggregate function runs
on top of it wasn't enough.

**The real fix** has to restructure the aggregation itself: compute the
finest real (operator, day_type) grain once from the raw observations,
then derive the pooled `'*'` rungs from that already-reduced result rather
than re-scanning raw rows four times. The model's own docstring rejected a
"median of medians" for statistical reasons (a pooled median must be a
true median over every journey in the pool) -- any restructure has to
keep that property, not trade it away for speed. Worth doing before
1.2.0: this is a live stability bug now, not a nice-to-have.

**Status (2026-09-15):** implemented, not yet verified by a real run. The
finest grain (int_first_visit_per_journey) and each of the four
operator/day_type rungs are now separate dbt models under
models/intermediate/ (int_line_stops_real_real, int_line_stops_real_star,
int_line_stops_star_real, int_line_stops_star_star), each independently
materialized as a table and contract-enforced against column drift.
build.sh's single dbt run is split so this five-model cluster runs at
--threads 1 -- a hard scheduling guarantee that only one grain's aggregate
state is ever open, rather than the unverified AS MATERIALIZED hint tried
first. Still open: no real dbt run/EXPLAIN ANALYZE has executed against
this shape yet, the docs/operations.md/README.md/bus-marts.odcs.yaml cron
references are still accurate (the cron trigger has not been swapped for
the prepared ops/systemd/wraith-bus-build service and timer yet), and the
route-reliability re-measurement this file's docstring calls for after any
by_grain change has not been re-run.

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
