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

## Now: 1.5.0 -- Android APK, LineageOS Tegu (Pixel 9a), Android 16+

Landed 2026-09-16. Full native, not a WebView wrapper -- Jetpack Compose
+ OkHttp, single-device scope (Tegu only, minSdk 36), two screens plus
push. `android/` is a real, standalone Gradle project alongside
`backend/`, `dbt/`, `ingestor/`, `archiver/` -- same "sibling app dirs,
not one src/ tree" shape `pyproject.toml` already documents for the rest
of this repo.

- **Tracking.** `ui/tracking/TrackingScreen.kt` -- a MapLibre map using
  the exact same tile source and style as `ghost-site`'s web bus map
  (`assets/style.json`, translated from that page's own style object, not
  a second cartography invented for the phone), live bus positions from
  `/buses` refreshed every 60s (matching the web map's own cadence),
  tap-to-query `/bus/<id>` in a bottom sheet, and a "Show route" action
  that draws `/route`'s line+stops overlay. A debounced search bar hits
  `/search` and recenters the camera. Zero new backend work, same
  endpoints the web map already uses.
- **Dashboard.** `ui/dashboard/DashboardScreen.kt` does NOT query QuestDB
  or Grafana directly from the phone -- it hits two new read-only Flask
  endpoints instead:
  - `/api/metrics/summary` -- feed age, buses tracked, positions/hour,
    hours since build, DQ issues. Each field is its own independent
    `qdb_exec` call and fails on its own, the same "one bad query
    shouldn't blank the others" reasoning `bus_detail()` already applies.
  - `/api/metrics/punctuality` -- system on-time % and median delay, ETA
    model fit, and the worst-lines table, straight off
    `line_punctuality_summary` and `eta_model_summary`, the exact tables
    and queries the Grafana dashboard's own "Punctuality & ETA models"
    row already uses.

  One API surface for every client (web, Grafana, phone); QuestDB stays
  off the public internet regardless of how Grafana ends up exposed.
  Both endpoints verified against live QuestDB with real data before any
  Kotlin was written (25,910 buses, 37.3% system on-time, line 320 the
  worst offender), and covered by two tests each: the degrade-quietly
  path (matches CI, where `QUESTDB_URL` is unset) and the real success
  path against a faked QuestDB response.
- **Push notifications.** `ntfy/NtfyForegroundService.kt` -- a foreground
  service holding a live OkHttp WebSocket to the same `ntfy` topic
  Grafana's alerts already publish to, posting a real Android
  notification per message. Doubling backoff on disconnect (2s -> 60s),
  the same shape `ingestor/ingest.py`'s own dead-feed backoff uses, just
  tuned faster since this is a live push channel, not a slow poll. The
  topic is a `BuildConfig` field sourced from `local.properties`
  (gitignored, `local.properties.example` committed as the template,
  same convention as this repo's own `.env`/`.env.example`) -- never
  hardcoded into a committed `.kt` file.
- **Built via ultracode, verified by a real compile, not just review.**
  A full Android SDK (platform 36, build-tools) and Gradle toolchain were
  set up specifically to compile this for real. Two real bugs only a
  compiler catches were found and fixed this way: the agent-written
  `TrackingScreen.kt` imported GeoJSON types from `com.mapbox.geojson`
  (MapLibre's historical upstream) when 11.x actually repackaged them
  under `org.maplibre.geojson` -- confirmed by pulling the real
  `android-sdk-geojson` jar and listing its classes, not guessed twice.
  Every hand-written XML comment that used `--` (this repo's own house
  style) had to be found and fixed, since XML forbids it in comments.
  `./gradlew clean assembleDebug` succeeds from a clean checkout: real
  APK, `com.thomaspeoples.wraithbus`, launchable `MainActivity`,
  `minSdk = targetSdk = compileSdk = 36`.
- **Dependency versions are pinned one wave back from bleeding-edge
  androidx, deliberately.** The latest `core-ktx`/Compose/`lifecycle`/
  `navigation-compose` releases as of this build all require compiling
  against API 37 -- which is still an active preview cycle on Google's
  SDK repository (`android-37.0`/`.1`/`.2`, no plain stable `37` yet),
  not the real, stable, shipping Android version this app's "Android
  16+ minSdk" actually means. Every dependency was rolled back one
  release to the last wave that targets stable API 36 instead of
  chasing a preview platform for a single-device app that ships once.

**Not built:** a signed release variant. This is a sideloaded,
single-device app with no store distribution -- the debug build is
already a real, installable APK, and there is nothing a release
signing config would buy here beyond ceremony.

**Shipped, then found broken on the real device, twice -- both fixed
same-day:**

- **The dashboard showed "No data" everywhere.** `/api/metrics/summary`
  404'd in production. Merging `feature/TJP-16090` to `main` never
  deploys `api` -- nothing in `.gitea/workflows/` rebuilds it, a gap
  that had simply never been hit before because nothing had changed
  `backend/app.py` and needed shipping on its own since the split from
  `ghost-site`. Fixed by rebuilding and recreating `ghost-backend` by
  hand and reloading `ghost-web`'s nginx; verified against the real
  public URL afterward, not just "container came up healthy." Full
  writeup: [ADR-0004](adr/0004-api-container-has-no-deploy-automation.md).
- **The map showed no buses.** Not a missing-data bug -- `/buses`
  returned 18,001 real features. 77% of them carry `status: "unknown"`
  (confirmed against the live feed), and `TrackingScreen.kt`'s
  `StatusUnknownColor` (`#6D7787`, correctly copied from the web map's
  own palette) is near-identical to `style.json`'s own `roads-major`
  color (`#6b7889`) -- most dots were camouflaged against major roads,
  not absent. The web map's own palette already has a `busStroke`
  (`#12161c`) that the Kotlin translation simply never carried over.
  Fixed by adding the missing stroke (verified against the real
  `PropertyFactory`/`CircleLayer` API by disassembling the compiled
  MapLibre jar with `javap`, not assumed) and bumping the circle radius
  slightly. `setGeoJson()` vs. `setGeoJsonSync()` was investigated as a
  second possible cause -- confirmed real (the jar exports both, with
  different threading semantics) but left as async on purpose: the feed
  runs to ~18k features GB-wide, and parsing that synchronously on the
  main thread every 60s would trade a visibility bug for a jank one.

**Logo redesigned.** The first cut (`ic_launcher_foreground.xml`) was a
bare orange rectangle with two dots for wheels -- functional, not a real
icon. Three independent design directions were generated (a realistic
coach silhouette, a bold geometric bus-front mark, a map-pin-with-bus
glyph), each rendered to PNG at both full size and actual 48px launcher
size with `resvg` and inspected before picking one -- the map-pin
candidate lost specifically at 48px, where its interior bus glyph turned
to mud. The coach silhouette won: most recognizable, holds up small,
fits the brand's palette. The shipped XML was verified by re-extracting
its own `pathData` values into a throwaway SVG and re-rendering *that*,
so what was inspected is provably what ships, not a hand-typed
approximation of it.

**About, FAQ, and an in-app update notice.** Reached from a new
overflow menu in a `TopAppBar` (`AppNav.kt`), keeping the primary bottom
nav to Tracking/Dashboard rather than crowding four tabs onto a phone
screen for two low-frequency destinations. About covers what the app is,
BODS/OpenStreetMap attribution (the OSM credit line is a real license
requirement of using that data, not decoration), the MIT license and
source link, and a plain privacy statement. FAQ covers 12 real questions
grounded in the app's actual behaviour -- why most buses show `unknown`
(roughly three-quarters, confirmed against the live feed, not a guess),
why there's no way to repoint the app at a different server, what the
push notifications actually are. `update/UpdateChecker.kt` checks this
repo's Releases for a newer `android-X.Y.Z-debug` tag once per cold
start and shows a dialog if one exists -- a courtesy for someone who
sideloaded directly, since Obtainium and F-Droid already watch the same
Releases page on their own without the app's help.

**F-Droid and Obtainium readiness.** Full writeup:
[docs/android-distribution.md](android-distribution.md). The short
version: Obtainium already works today against this repo's existing
Releases (no code changes needed -- it was already shipping real
releases in exactly the shape Obtainium expects). F-Droid is split into
two different things, and only one was in scope here: the *public*
F-Droid catalog needs a merge request against a third-party project
(`fdroiddata`) that only the repo owner should decide to open, so that
was **not done**; what *was* done is everything the app itself needs to
be F-Droid-buildable --
`android/fastlane/metadata/android/en-US/` (title, descriptions,
per-`versionCode` changelogs, a 512×512 store icon rendered from the
same vector artwork as the launcher icon), a real reproducible-build
story (`local.properties` absent -- exactly F-Droid's build-server
state -- degrades to safe defaults, not a broken build), no proprietary
dependencies, and `versionCode`/`versionName` promoted to the actual
source of truth for the app's version rather than the release-tag names.
That last change surfaced a real inconsistency: `versionCode` had never
actually been bumped for the `1.0.0` -> `1.0.1` re-ship, both silently
carrying `versionCode 1` -- fixed by setting this release to the true
next value, `2`, rather than inventing a `3` to match the tag-name count.

**Also removed:** `ACCESS_COARSE_LOCATION`/`ACCESS_FINE_LOCATION`,
merged in transitively by MapLibre's own manifest for a "show my
position" feature this app never enables. Stripped via
`tools:node="remove"` rather than carried silently -- an app requesting
location it doesn't use is exactly what an F-Droid reviewer would flag,
so it's fixed rather than left for one to catch.

**Shipped, then found broken on the real device again, once -- fixed
same-day:**

- **Route line was nearly invisible, on both the phone and the web map.**
  `TrackingScreen.kt`'s `RouteLineColor` (`#9AA4B2`) was copied correctly
  from `ghost-site`'s own `MAP_PALETTE.dark.route` -- but that value was
  itself wrong: identical to the palette's `sibStroke` and close enough
  in lightness to `style.json`'s `roads-major` (`#6b7889`) to disappear
  into ordinary roads at any zoom. Not a translation bug this time, an
  upstream one. Fixed in both places: `MAP_PALETTE` (`bus-map/index.html`
  in `ghost-site`, a separate repo) moved to a saturated teal/cyan no
  other layer uses (`#0f7a91` light, `#4fd7e8` dark), the route line's
  width and opacity both went up, and the Kotlin copy was updated to
  match the dark value so phone and browser read the same route color
  again.
- **Tapping a bus required an extra tap to see its route, and showed raw
  ISO timestamps.** `BusDetailSheetContent` had a "Show route" button
  that fetched and drew `/route` only after the sheet was already open --
  backwards from what's actually useful, since the sheet then covers the
  bottom half of the map the route just appeared on. Reworked into one
  `selectBus()` path (shared by a map tap and a bus-number search hit,
  see below): fetch the bus, fetch and draw its route, fit the camera to
  the route's bounds, *then* open the sheet. The button is gone -- there's
  nothing left for it to do. ETA/predicted-arrival/next-stop-ETA fields
  now format through `isoToLocalTime()` (`java.time`, no desugaring
  needed at `minSdk 36`) instead of printing the raw
  `2026-09-16T18:57:39Z` the API returns.
- **Search only ever matched localities.** `/search`'s `lines`/`bus_ids`
  fields were already computed server-side and already parsed into
  `SearchResult` client-side -- just never rendered. A query like "X93"
  matches no locality at all, so the existing locality-driven line lookup
  (`marts['line_loc']`) never fired for it either; searching a route
  number returned nothing. Backend now also matches live line codes
  directly against the query (`/search`, independent of any locality
  match), and the search results card renders `lines`/`bus_ids` above the
  locality matches -- tapping a line fetches and fits its route, tapping
  a bus number runs the same `selectBus()` path a map tap does. Covered
  by `test_search_matches_line_number_directly`.
- **From About or FAQ, the bottom nav's Dashboard/Tracking icons did
  nothing.** `navigateToTab()`'s `popUpTo(startDestination){ saveState =
  true }` / `restoreState = true` pairing is the right pattern for
  several persistent, parallel tab stacks -- this app has one, with
  About/FAQ sitting two hops above it rather than beside it, so a stale
  saved back-stack entry could swallow the tap. Bottom-bar taps now hard
  reset to a single entry (`popUpTo(startDestination){ inclusive = true
  }`, no saved state) regardless of where they're tapped from -- can't
  get stuck the same way. Overflow-menu taps to About/FAQ stayed a plain
  push (`navigateToSecondary()`, `launchSingleTop` only, no `popUpTo`):
  resetting those too would have "fixed" the bug by breaking the system
  Back button instead, since with nothing left below About on the stack,
  Back would exit the app rather than return to Tracking or Dashboard.

**Where the bus data comes from, for the record (asked while chasing
the above):** live vehicle positions are BODS's SIRI-VM feed, polled
every 60s -- there is no static timetable/GTFS/TransXChange ingest at
all. Stop sequences and route geometry are inferred from where live
buses have actually been seen, matched to the nearest real stop in the
NaPTAN register (`dbt/models/staging/naptan/`, the same 435k-row GB-wide
register every UK transport app draws stop identity from) and snapped to
roads via a self-hosted OSRM. That's a different approach from
timetable-first apps like
[billthefarmer/buses](https://github.com/billthefarmer/buses) (which
reads BODS's separate TransXChange timetable dataset directly) -- this
app's routes are "what buses actually did," not "what the timetable
says they should do," which is also why a route can come back truncated
or `note`d as merged-suspect for a thin-data line.

## 1.6.0 -- F-Droid public submission

Not started. `docs/android-distribution.md` (landed in 1.5.0) already
covers everything the *app itself* needs to be F-Droid-buildable --
fastlane metadata, a reproducible build with `local.properties` absent,
no proprietary dependencies, `versionCode`/`versionName` as the real
source of truth. This milestone is the other half: actually submitting
to the public catalog, per F-Droid's own
[Submitting to F-Droid quick start](https://f-droid.org/docs/Submitting_to_F-Droid_Quick_Start_Guide/).

**What is and isn't the blocker, stated plainly.** Self-hosting the
backend is not the problem -- that's the whole premise of this project,
and it's exactly what Obtainium + the self-hosted F-Droid repo (both
already shipped in 1.5.0) already deliver: a free, real app, sideloaded
or installed via F-Droid-the-client, hitting one person's own server.
Nothing about that requires a listing in F-Droid's *public, centrally
searchable* catalog (`fdroiddata`/`f-droid.org`) -- that catalog is
specifically for apps a stranger finds by searching and expects to be
general-purpose. The API base URL is baked in at build time
(`BuildConfig.API_BASE_URL`, defaulting to
`https://www.thomaspeoples.com/api/`) and not repointable
(`ui/faq/FaqScreen.kt`: "Can I point this app at a different server?
No."), so a stranger who found this in the public catalog would get a
permanent client for someone else's bus feed -- likely a review
rejection, and not very useful even if accepted. Three ways through,
before opening a merge request:

- **Ship it anyway, clearly labelled.** Description and metadata state
  outright that this is a demo/personal-infrastructure client, not a
  general-purpose bus tracker -- honest, but likely still a rejection on
  review, and even if accepted, of near-zero use to anyone who installs
  it from search.
- **Make the backend URL genuinely runtime-configurable** (a first-run
  settings screen, not a rebuild) so the F-Droid build is a real general
  client anyone can point at their own deployment. The correct fix if
  this milestone is worth doing at all, but it's new app functionality,
  not a submission checklist item -- scope it as its own piece of work
  first.
- **Don't submit to the public catalog; stay on the self-hosted F-Droid
  repo path `android-distribution.md` already chose.** Zero new app
  work, zero review risk, and the app never claims to be something it
  isn't. The default until one of the above is deliberately chosen.

**If runtime-configurable backend is built and the decision is to
proceed, the mechanical submission steps are:**

1. Fork [`fdroiddata`](https://gitlab.com/fdroid/fdroiddata) and add
   `metadata/com.thomaspeoples.wraithbus.yml` describing how to build
   from this repo's public Gitea URL: `Categories`, `License: MIT`,
   `SourceCode`, `IssueTracker`, and a `Builds:` entry per `versionCode`
   pinning the exact commit, `gradle: yes`, `output:` the built APK path.
2. Verify locally with `fdroid build --local
   com.thomaspeoples.wraithbus:<versionCode>` (requires `fdroidserver`,
   not installed anywhere in this stack today) -- must succeed from a
   clean clone with no `local.properties`, proving the same "F-Droid's
   build-server state" story `android-distribution.md` already
   documents for the self-hosted repo path.
3. Run `fdroid checkupdates --auto` to confirm the metadata's
   update-detection matches this repo's real tagging scheme
   (`android-X.Y.Z-debug` release tags).
4. Open a merge request against `fdroiddata` with the new metadata file.
   F-Droid's own reviewers take it from there -- expect requests for
   changes; this is a third-party project's process, not one this repo
   controls.
5. Once accepted and the first build lands, add an F-Droid badge/link to
   `README.md` and `docs/android-distribution.md`, and drop this repo's
   Gitea Releases page as the primary distribution channel down to
   secondary (F-Droid's own update mechanism supersedes
   `UpdateChecker.kt`'s courtesy check for anyone who installs it that
   way).

**Not in scope for this milestone regardless of the decision above:**
standing up `fdroidserver` for a *second*, official F-Droid repo (the
self-hosted repo already covers "install via F-Droid app" for this
project's own use); Play Store distribution (never planned, contradicts
this project's whole self-hosted premise).

## 1.7.0 -- home screen widget

Not started. Ordered after 1.6.0 on purpose -- do not build before then.

**Warning, read before picking this up:** a live widget needs either
`ACCESS_COARSE_LOCATION` (idea 1 below) or a background-refresh path
tighter than Android's 15-minute `WorkManager` floor (idea 2) -- both
are real scope, not a quick add-on, and both are currently things this
app deliberately does *not* hold (see 1.5.0: location permissions were
stripped from the manifest specifically because the app carried them
unused). Don't start this without picking one of the three ideas below
first and accepting its specific cost; "just add a widget" is not a
bounded task.

Ideas, for whenever this gets picked up:

1. **Nearest stop, next departure.** Glance widget reading the device's
   last-known location and showing the next 1-2 predicted arrivals at
   the nearest stop. Needs `ACCESS_COARSE_LOCATION` -- a real tradeoff,
   not a free add, for an app whose FAQ currently says outright "no,
   this app does not request or use your location."
2. **Pinned line.** Pick one line/direction once in-app; the widget shows
   its live buses' ETAs to a chosen stop. A live-feeling refresh needs
   `AlarmManager` or a foreground-adjacent update path -- vanilla
   `WorkManager`'s 15-minute floor makes ETAs stale before they're read.
3. **Fleet health strip.** No location or line picker at all -- just
   `/metrics/summary` (feed age, buses tracked) as a small always-current
   strip. Cheapest to build (reuses `DashboardScreen`'s existing endpoint
   verbatim), least useful day to day.

(1) is the one worth having; (2) is what a habitual rider actually wants
but costs the most to keep live; (3) is the fallback if neither the
location permission nor the background-refresh plumbing is worth it.
Pick one before this becomes real work, not just a scope.

## 1.8.0 -- split the Android app into its own repo

Not started. `android/` has been described as "a real, standalone Gradle
project alongside `backend/`, `dbt/`, `ingestor/`, `archiver/`" since it
landed in 1.5.0 -- this milestone is making that literally true: its own
Gitea repo, not a sibling directory inside `wraith-bus-ingestor`. This
splits the *app* out from the *platform* (backend/API + ingest/dbt);
`ghost-site` (the PWA website) is already its own repo today and is
unaffected either way.

Real things this touches, not just a `git mv`:

- **History.** A plain `git mv` loses `android/`'s commit history from
  the new repo's perspective (it'd show up as one giant initial commit).
  `git subtree split` or `git filter-repo --path android/` preserves it
  properly -- decide which before doing this, not after.
- **Release tags and Obtainium.** `android-X.Y.Z-debug` tags currently
  live in *this* repo's Gitea Releases, and both
  `UpdateChecker.kt` (`git.thomaspeoples.com/api/v1/repos/thomaspeoples/
  wraith-bus-ingestor/releases`) and Obtainium's own config point at
  this repo's URL. All three move to the new repo's URL together, in one
  change -- an app pointed at the old repo's releases would silently
  stop seeing updates otherwise.
- **`docs/android-distribution.md` and the fastlane metadata.** Decide
  whether Android distribution docs move with the app repo or stay
  documented from the platform side with a link across -- this repo's
  own `mkdocs.yml` nav currently lists it as platform documentation.
- **The `style.json` palette coupling.** `TrackingScreen.kt`'s map style
  and colours are already a hand-kept translation of `ghost-site`'s own
  `MAP_PALETTE` (see the route-colour fix earlier this session) --
  today that's a two-repo sync problem; after this split it's a
  three-repo one. No new mechanism proposed here, just naming the cost
  going up before it happens.
- **Versioning.** The app repo needs its own `versionCode`/`versionName`
  authority and its own release automation, independent of this repo's
  `pyproject.toml` version and `.cz.yaml` `bump_map` -- it already
  conceptually has this (`versionCode`/`versionName` in
  `build.gradle.kts`, not derived from the platform's version), just not
  its own CI yet.
- **`API_BASE_URL`.** Unaffected by the split either way -- it's a
  build-time constant pointing at this platform's public API regardless
  of which repo the Kotlin lives in.

## Considered, not started -- static timetable ingest (TransXChange)

Raised while comparing this app's routing to
[billthefarmer/buses](https://github.com/billthefarmer/buses), which
reads BODS's separate TransXChange (timetable) dataset directly rather
than inferring routes from live positions the way this project does.
Real tradeoff, not a strict upgrade -- see the ROADMAP entry under
1.5.0 ("Where the bus data comes from") for what's live-inferred today,
and the chat discussion this was raised in for the fuller pro/con case:
TransXChange would give scheduled (not just live-observed) stops and
times, and routes for lines with too little live data to infer today --
at the cost of a second, much larger ingest (TransXChange is verbose
XML, one file per operator per service, GB-wide) and a second source of
truth for routes that can disagree with what buses actually do. No
decision made; not scheduled against a version number yet.
