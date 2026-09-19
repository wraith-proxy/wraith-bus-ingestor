# Roadmap

Where this is now, and what's next. Not a schedule -- just the order
things need to happen in, revised as the plan changes.

These `1.x.y` headings are planning milestones, not release tags. The `v*`
tags `cz` cuts on every merge to `main` are an independent sequence --
`.gitea/workflows/release.yml` bumps them per merge, so the two numbers
drift apart by design and neither is wrong.

## 1.0.0 -- single-NAS deployment, data owned

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
but that's docs, not a gated build/test.

**Former blocker, resolved in 1.5.0:** `ghost-runner` (Alpine/musl)
couldn't run dbt at all -- `dbt-core-experimental-parser` shipped no
musllinux wheel and refused to build from source on any platform. 1.5.0
moved `ghost-runner` to Debian/glibc, so a job on `ghost-runner` is a real
option now, not just `ubuntu-latest`. Neither is done yet -- this entry is
still open, just no longer platform-blocked. Whichever runner it lands on,
the job runs `dbt build` + `dbt test` and fails the pipeline on a broken
model or a failed assertion.

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
  `/mnt/archive`, idempotent; Parquet-based since ADR-0007) and `replay <start> <end>` (restore-if-needed
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

Both of this section's open items closed in 1.4.0. `replay` turned out to
be broken rather than merely untested -- see 1.4.0 below and
[ADR-0003](adr/0003-replay-verified-by-construction.md), which also
records why the full end-to-end rehearsal was deliberately not run.

## 1.4.0 -- Grafana: observability, DQ, alerting

Landed 2026-09-15. QuestDB already speaks the Postgres wire protocol on
8812, so Grafana's built-in Postgres datasource points straight at it and
live metrics are a direct query -- no plugin, no second pipeline, no
hand-rolled semantic layer.

- **DQ / test results.** `dbt test` used to print to `build.log`, gate
  the publish, and be forgotten. `dbt/publish_run_results.py` now reads
  `target/run_results.json` -- structured, and written even when the run
  fails, which `build.log` parsing would not have given us -- and pushes
  `dbt_test_results` (per test) and `dbt_build_runs` (per run) into
  QuestDB over ILP, reusing the `/write` transport `ingestor/ingest.py`
  already uses rather than adding a dependency. Not duckdb: it has no
  wire protocol Grafana can reach.
- **`build.sh` had to stop dying before it could record a death.**
  `set -e` exited on a failed `dbt test`, so test failures -- the single
  most valuable rows for a DQ dashboard -- were the one case that could
  never be written. It now captures the exit code, publishes either way,
  then re-raises. The publish gate is unchanged: a red build still leaves
  the previous good marts serving.
- **`replay` was deleting its own recovered data.** Both incremental
  models' retention post-hooks prune to `max(date) - 8 days`, and
  `replay` only restores days older than `today - RETENTION_DAYS`. Same
  boundary: every day old enough to need restoring was old enough to be
  pruned again in the same run. Confirmed by measurement, fixed with a
  `replay` dbt var, and verified by parsing the project both ways. See
  [ADR-0003](adr/0003-replay-verified-by-construction.md).
- **Alerting.** Grafana Alerting -> self-hosted `ntfy` via a webhook
  contact point, not email. Three rules, deliberately not one per test:
  feed stale (nothing in `bus_positions` for 15 min), nightly build
  missing or failed (the silent-cron class that went unnoticed for weeks
  after the September split), and dbt tests failed **or warned** --
  warned matters, because a warn exits the build green and was exactly
  the gap this milestone existed to close. All three provision and
  evaluate to `health=ok` against live QuestDB, and delivery is verified
  end to end, not just configured: a message published server-side from
  Grafana to `ntfy` was read back off the topic.
- **Everything is file-provisioned.** Datasource, dashboard, contact
  point and rules all live in `ops/grafana/provisioning/`, with
  `allowUiUpdates: false`, so the UI cannot drift away from git. The
  dashboard's 19 panels were each run against live QuestDB before being
  committed.
- **Grafana is pinned to `11.6.16`, not `:latest` -- this cost a real
  incident to learn.** `:latest` first shipped 13.x's "Dynamic
  dashboards", GA'd and on by default 2026-04-08: a provisioned
  classic-schema dashboard renders (layout, titles, panels, all correct)
  but the frontend never issues the query request at all -- confirmed
  live by watching nginx's access log across four full page loads from a
  real device: zero `/api/ds/query` calls, ever. Every panel showed "No
  data". Known upstream issue, not specific to this dashboard:
  [grafana/grafana#122663](https://github.com/grafana/grafana/issues/122663).
  Pinning to `12.3.11` did not fix it -- that version runs the same
  apiserver-backed dashboard path under a `v1beta1` API instead of `v2`,
  and setting `GF_FEATURE_TOGGLES_KUBERNETESDASHBOARDS=false` (and the
  three `dashboardScene*` toggles) had no effect: Grafana reported them
  back as `true` regardless, meaning this toggle is locked at General
  Availability, not merely defaulted on. `11.6.16` predates Dynamic
  Dashboards existing in any form (introduced in 12.0, May 2025) -- both
  the `v1beta1` and `v2` dashboard-dto endpoints genuinely 404 on it, not
  just hidden behind a flag. Do not bump this image without checking
  `/apis/dashboard.grafana.app/` 404s on the target version first.
- **The `/grafana/` redirect loop, and what actually caused it.** After
  first wiring the nginx location, the dashboard hit
  `net::ERR_TOO_MANY_REDIRECTS` in a real browser. First fix attempt --
  hardcoding `X-Forwarded-Proto: https` because the Cloudflare tunnel
  forwards plain HTTP and `$scheme` is always `http` here -- was a real
  bug (confirmed: with `$scheme`'s `http` header Grafana 301'd, with
  `https` it 200'd) but did not stop the loop through nginx. Actual
  cause: `proxy_pass http://upstream/;` (trailing slash) strips the
  matched `/grafana/` prefix before forwarding, but
  `GF_SERVER_SERVE_FROM_SUB_PATH=true` means Grafana expects that prefix
  preserved on every request -- stripped, it 301s to re-add it, and
  nginx strips it again on the retry. Fixed by dropping the trailing
  slash and path from `proxy_pass` in both `/grafana/` locations, so
  nginx forwards the full original URI unchanged. This lives in
  `ghost-site`'s nginx template, not this repo, but the two settings
  that have to agree (`GRAFANA_ROOT_URL`, `GF_SERVER_SERVE_FROM_SUB_PATH`)
  are both here -- see [Architecture](architecture.md).
- **Punctuality & ETA model panels**, added after the first cut landed.
  `line_punctuality` and `eta_models` are real dbt marts (median/p90
  delay per line, regression fit per grain) that were already computed
  but, like `dbt test` results, lived only in duckdb -- unreachable from
  Grafana. `dbt/publish_punctuality_summary.py` collapses both into
  QuestDB after every `build.sh` publish: one row per line and one
  system-wide model-fit summary. "On-time" uses the DfT Traffic
  Commissioner standard for local bus services -- 1 minute early to 5
  minutes 59 seconds late, 95% the statutory target -- not an invented
  threshold. The rollup is a volume-weighted approximation from
  grain-level medians, not a true per-journey percentage, because no
  per-journey delay is stored anywhere in this pipeline; both the script
  and every panel description say so.
- **Public hosting.** Anonymous `Viewer` for Grafana behind
  `/grafana/` on `ghost-site`'s nginx, the same `location` shape
  `/tiles/` and `/api/` already use. Verified: anonymous read returns
  200, anonymous write returns 403. `ntfy` gets `/ntfy/` but
  **subscribe-side only** -- `limit_except GET HEAD` denies the publish
  side, so a POST from the internet is 403 while Grafana still publishes
  server-side over `ghost-net`. That means the stock ntfy Android app
  works today; 1.5.0's APK reuses the same topic rather than inventing a
  delivery path. `slaycoach.quest/oma` 302-redirects to the dashboard
  directly -- a redirect, not a proxy, since Grafana's
  `GF_SERVER_ROOT_URL` is fixed to the `thomaspeoples.com` domain and
  serving it under a second domain would break its own generated asset
  URLs.
- **Secret hygiene, verified by grep rather than assumed** -- as this
  section demanded before it was allowed to close. The topic name is the
  only credential: absent from both repos' full git history, absent from
  both working trees, absent from container logs, and absent from the
  public API surface. Both `.env` files are untracked. Grafana's env-var
  interpolation was also confirmed to resolve inside the *alerting*
  provisioner (documented only for datasources), by reading the contact
  point back from the API -- verified across every version tried in the
  pin saga above, `11.6.16` included.

**Deliberately skipped:** Grafana annotations for past incidents. The
plan called for them, nothing consumes them today, and the volume is
there whenever a real incident wants marking.

**The Android app moved to its own repo.** 1.5.0 through 1.8.0 (the
APK build, F-Droid submission, the home-screen widget, and the repo
split itself) used to live here; they're now
[wraith-bus-ingestor-apk](https://git.thomaspeoples.com/thomaspeoples/wraith-bus-ingestor-apk)'s
`docs/ROADMAP.md`, moved verbatim on `feature/TJP-17091` when `android/`
left this repo on `feature/TJP-17090`. See 1.9.0 below for the most
recently landed milestone, and "Considered, not started" for what's raised
but not scheduled.

## Considered, not started -- static timetable ingest (TransXChange)

Raised while comparing this app's routing to
[billthefarmer/buses](https://github.com/billthefarmer/buses), which
reads BODS's separate TransXChange (timetable) dataset directly rather
than inferring routes from live positions the way this project does.
Real tradeoff, not a strict upgrade -- see the Android app repo's
`docs/ROADMAP.md` ("Where the bus data comes from," under its 1.5.0)
for what's live-inferred today, and the chat discussion this was raised
in for the fuller pro/con case:
TransXChange would give scheduled (not just live-observed) stops and
times, and routes for lines with too little live data to infer today --
at the cost of a second, much larger ingest (TransXChange is verbose
XML, one file per operator per service, GB-wide) and a second source of
truth for routes that can disagree with what buses actually do. No
decision made; not scheduled against a version number yet.

## 1.9.0 -- ghost-runner off Alpine, dbt v2 + DuckDB 2.0 alpha migration (reverted)

(1.5.0-1.8.0 are taken -- see "The Android app moved to its own repo" below.)

Landed 2026-09-17, **reverted 2026-09-18**. Supersedes both the "dbt 2.0
migration" and "DuckDB 2.0 migration" entries this section used to carry --
neither panned out the way they were written, and the replacement didn't
pan out either. Full investigation, decision, rollback, and consequences
written up in [ADR-0005](adr/0005-dbt-v2-duckdb-alpha-migration.md) rather
than repeated here.

Short version: the `ghost-runner` musl blocker was real and confirmed
twice (dbt v1's and v2's own hard deps both ship stub-only sdists with no
musl wheel target), so `ci-images` moved to Debian -- **that part stands**.
`dbt==2.0.4` + `duckdb==2.0.0.dev2609121639` were pinned as real
dependencies, verified clean in isolation (884/884 tests, a real
`build.sh` run), then rolled back the next day after `wraith-bus-
build.service`'s actual nightly conditions (full 8-day extract,
`--threads 1`, the 86-model `line_stops` cluster back-to-back with the
main run) produced a real memory blowup isolated testing never caught --
see the GOTCHA below. Back on `dbt-core==1.12.4` + `dbt-duckdb==1.11.0`.

### GOTCHA: isolated dbt verification does not catch production-load memory blowups

`dbt run` + `dbt test` passing clean, even against real data, is not the
same test as the actual nightly build path. Four real OOM-kills on
2026-09-18 (`MemoryMax` raised 9000M -> 12000M as an explicit
measurement pass along the way) confirmed, via the kernel oom-killer log
itself (`anon-rss` at kill, not page cache), that dbt v2's bundled
DuckDB 2.0 alpha engine's real peak memory need for this pipeline's
`line_stops` phase sits well above 12GB on a 15GB host -- `profiles.yml`'s
`memory_limit: 6GB` was nowhere near honored by actual RSS. Full
timeline in [ADR-0005's Rollback section](adr/0005-dbt-v2-duckdb-alpha-migration.md#rollback-2026-09-18).

**Takeaway that outlives this specific migration:** before trusting any
future engine/adapter version bump on this pipeline, rehearse it under
the real `systemctl`-triggered nightly conditions and `MemoryMax` cap,
not just an isolated `dbt run`/`dbt test` pass -- the gap between the two
is where this one hid.

**Remaining:**
- ROADMAP 1.1.0 (gate dbt in CI) is unblocked by the Debian base
  regardless of dbt version, not closed -- a `dbt build`/`dbt test` job
  can run on `ghost-runner` itself now, but nothing adds that job yet.
- `pages.yml`'s `dbt docs generate` could move off `ubuntu-latest` onto
  `ghost-runner` now that the platforms match; not done as part of this
  migration.
- dbt v2 / DuckDB 2.0 alpha themselves: not retried until DuckDB 2.0
  reaches a real GA with production mileage elsewhere, and only with the
  full-scale rehearsal above included from the start.

## 1.10.0 -- bus_positions schema optimisation

Landed 2026-09-18. Full investigation, decision, and measured outcome in
[ADR-0006](adr/0006-bus-positions-schema-optimisation.md). Short version:
`bus_positions` went from 44GB to 21.6GB on disk (~51%, QuestDB's own
`diskSize` accounting) -- `vehicle_id` (100% NULL, dead) dropped,
`lat`/`lon`/`bearing` `DOUBLE` -> `FLOAT`, seven wildly low-cardinality
`VARCHAR` columns -> `SYMBOL`. All verification gates passed before
cutover: exact row count match, sub-metre `FLOAT` precision loss,
exact `SYMBOL` distinct-count match, full `dbt run`+`dbt test`
(884/884) against data extracted from the new table before it went
live. **Cost, found 2026-09-19:** the 63 days archived before the migration
can no longer be decoded (their SYMBOL values went with the dropped old
table). The owner approved the breaking change and judged it the wrong call;
it is irreversible. See the ADR, and 1.11.0 for what was done about the
archive.

**Remaining:**
- `bus_positions_old` was dropped early (2026-09-19), not after the planned
  burn-in -- there is no old-schema hot table to fall back to.

## 1.11.0 -- Parquet archive

Landed 2026-09-19. Full decision, measurements and the bugs found in
[ADR-0007](adr/0007-parquet-archive.md). The archive was raw QuestDB partition
directories, which ADR-0006 showed cannot survive their source table being
dropped. Every archived day is now one self-describing Parquet file
(`bus_positions_parquet/<day>.parquet`, zstd level 3): a full 34M-row day is
0.69GB against ~3.0GB as a QuestDB partition. The archiver exports over
QuestDB's HTTP API, reads the file back with DuckDB, and drops the live
partition only when it matches row for row. `ops/incident.py restore` loads a
file back (fingerprint-checked on 2026-09-11) and the archive can be queried in
place with DuckDB. Each run records itself in `archiver_runs`, and two Grafana
rules (no clean run in 36h; latest run needs attention) reach the phone via
ntfy.

**Remaining:**
- The 68 raw partition directories were deleted on 2026-09-19 at the owner's
  instruction, after every day had a verified Parquet file and an independent
  recount of the 63 salvaged days matched (manifest on the archive drive).
  Irreversible; the archive is now 68 Parquet files, 10GB (was 46.7GB).
- 63 days (2026-07-03 .. 2026-09-06) are numeric-only Parquet: `timestamp`,
  `lat`, `lon`, `bearing` and raw `bus_id_key`/`line_key`. Their bus and line
  identities are gone for good.
- The archiver depends on QuestDB's `/exp?fmt=parquet` and `read_parquet()`,
  and the image is `questdb/questdb:latest`. A release that changes either
  shows up as an `error` run (nothing is dropped unverified), not corruption.

## Known issues -- raised, not yet actioned

**Open decision: the ingest bounding box.** It was widened from the
north-west of England to all of mainland GB on 2026-09-12 (4.72x the
vehicles; rows/day ~3M -> ~13M). Every capacity number in this repo that
predates that (`memory_limit`, `MemoryMax`, QuestDB disk, nightly runtime)
was sized for the old volume -- see the GOTCHA in `docs/operations.md`.
Deliberately left undecided (2026-09-19): keep GB-wide and retune upwards,
or narrow back to the area actually watched. Narrowing would shrink disk,
memory and runtime at the source.

**The 2026-09-19 03:00 nightly failed and nothing published.** DuckDB's own
4GB `memory_limit` was exhausted in `int_pings_matched_to_stops` (not the
cgroup cap, which held), so the API is serving marts from 2026-09-18 06:46.
Cause not isolated. TJP-19090 removes one contributor (12 days / 81.9M rows
were being read instead of 8 because `extract.py` never pruned old parquet),
but that is a hypothesis until a real `build.sh` passes at 4GB.

**The archiver jam on an already-archived day is fixed (TJP-19090).** A
restored day colliding with its own archive copy used to stop the whole run
and block everything behind it (nothing archived 2026-09-16 to 2026-09-19).
It now reads the existing Parquet file back: matching rows drop the live copy,
a mismatch skips the day and raises the `attention` alert, and the run carries
on. Any other failure (an export that fails verification, a failed DROP, a
missing sentinel) still stops the run, by design, and is recorded as `error`.
See the GOTCHA in `docs/operations.md`.

Observed after a container restart cleared a memory-pressure symptom on
the host. System memory barely moved (4,793M -> 4,751M available)
because the freed 1.3GB went back to page cache rather than showing as
free, and swap is still at 4,222M -- that's accumulated pressure that
won't clear without the swapped-out pages being touched again. Neither
blocked anything at the time.

**The swap half of this is now actioned, 2026-09-18** (see
[ADR-0005](adr/0005-dbt-v2-duckdb-alpha-migration.md)'s aftermath --
tonight's dbt v2 rehearsal work hit the same accumulation for real,
5.5GB resident, tight enough to threaten the next build). A new
`wraith-bus-precleanup.timer` runs `ops/precleanup.sh` at 02:00, one
hour ahead of the 03:00 build: safety-checked `swapoff`/`swapon` per
swapfile (only attempted if `MemAvailable` actually covers that file's
used bytes plus a 1GB margin -- skips and logs rather than forcing a
too-tight reclaim) plus a dangling-image/unused-build-cache Docker
prune. Soft-fail throughout; a skipped or failed step never blocks the
03:00 build. See `ops/precleanup.sh` and
`ops/systemd/wraith-bus-precleanup.service`/`.timer`.

The underlying DuckDB buffer creep is still unaddressed -- a nightly
swap reclaim resets the symptom, not the cause. It'll climb again over
days. Still open.
