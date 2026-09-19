# Operations

## The nightly rebuild

`dbt/build.sh` runs from a host systemd timer at **03:00** every night —
`ops/systemd/wraith-bus-build.{service,timer}`, installed to
`/etc/systemd/system/` (a host unit, not a container, since it needs the
repo root's `.venv` environment). Replaced the old host crontab entry
2026-09-15 — the unit also auto-stops `ghost-osrm`/`ghost-backend`/
`ghost-tiles` for the build window and restarts them after
(`ExecStartPre`/`ExecStopPost`), which cron never did:

```
sudo systemctl status wraith-bus-build.timer
sudo systemctl list-timers wraith-bus-build.timer
journalctl -u wraith-bus-build.service
```

A run does four things in order: extract the last `EXTRACT_DAYS` (default
22) of `bus_positions` out of QuestDB into `data/*.parquet`, `dbt run`
every model into a scratch warehouse, `dbt test` the result, and — only if
the tests pass — publish the five marts into `wraith_bus_ingestor.duckdb`, the file
the API reads. A failed test run leaves the previous night's marts serving
rather than replacing them with something known to be wrong.

The whole thing runs under `nice -n 15` (and `ionice -c3` where available):
DuckDB will happily take every core on the box, and unconstrained it drove
load past 10 on the same 6-core host that also runs QuestDB, the ingestor
and the API — pushing the API's p50 from 1.3s to 15.6s, long enough for
nginx to start returning 504s. A slower rebuild is an acceptable cost; the
site falling over during it is not.

### The 02:00 precleanup, one hour ahead

`ops/systemd/wraith-bus-precleanup.{service,timer}`, added 2026-09-18,
runs `ops/precleanup.sh` an hour before the build: a safety-checked
`swapoff`/`swapon` per swapfile (only attempted if `MemAvailable` actually
covers that file's used bytes, skipped and logged otherwise — see the
script's own comments) plus a dangling-image/unused-build-cache Docker
prune. Closes the swap half of docs/ROADMAP.md's "Known issues" entry —
swap that accumulates during the day never clears on its own, and a tight
host makes the 03:00 build itself more likely to hit its `MemoryMax`
cgroup cap. Every step soft-fails: a skipped or failed reclaim never
blocks the build that follows it.

```
sudo systemctl status wraith-bus-precleanup.timer
journalctl -u wraith-bus-precleanup.service
```

### The three-file handoff

`build.sh` never lets dbt build the file Flask has open. DuckDB holds an
exclusive lock for the whole run, so an earlier version that built in
place made every read-only open from the API fail with `Conflicting lock
is held`, greying out every bus on the map for the run's duration.

Instead:

1. `dbt run` builds into **`wraith_bus_ingestor_build.duckdb`**, a persistent
   scratch warehouse (see below for why it persists).
2. Once `dbt test` passes, the five marts are copied into a fresh
   **`wraith_bus_ingestor_staged.duckdb`**.
3. `wraith_bus_ingestor_staged.duckdb` is `mv`'d over **`wraith_bus_ingestor.duckdb`** — atomic
   within a filesystem, so the API sees either the whole old file or the
   whole new one, never a lock or a partial write.

### Why the scratch warehouse persists across runs

`wraith_bus_ingestor_build.duckdb` used to be wiped before every run. Two of the
models — `int_pings_segmented_to_journeys` and `int_pings_matched_to_stops` — are incremental,
and dbt's `is_incremental()` only takes the fast path when the target
table **already exists**. Wiping the file every run silently turned
"incremental" into "full rebuild every time", which is exactly what
happened until 2026-09-13 and is why fixing a single stale test used to
cost a 20–30 minute rebuild. The file now survives between runs by
default; a normal night reprocesses only the trailing ~2 days for those
two models.

### Forcing a from-scratch rebuild: `FULL_REFRESH=1`

```bash
cd dbt
FULL_REFRESH=1 ./build.sh
```

Wipes `wraith_bus_ingestor_build.duckdb` and passes `--full-refresh` to `dbt run`,
forcing both incremental models to rebuild from nothing. Use this after a
schema change to either model, or if incremental drift is suspected — not
routinely, since it's the slow path the persistence above exists to avoid.

Other flags:

```bash
SKIP_EXTRACT=1 ./build.sh   # reuse the parquet already in data/, skip the QuestDB pull
REPLAY=1 ./build.sh         # disable the incremental models' retention post-hooks
                            # (set by ops/incident.py — see below)
```

### Test results are recorded, pass or fail

Straight after `dbt test`, `build.sh` runs `publish_run_results.py`, which
reads `dbt/target/run_results.json` and writes two tables into QuestDB over
ILP: **`dbt_test_results`**, a row per test with its status, failure count
and execution time, and **`dbt_build_runs`**, a row per run with the
overall result, elapsed seconds, model count and the passed/failed/warned
test counts. `run_results.json` rather than `build.log` because it is
structured, versioned by dbt, and written even when the run fails.

The ordering in `build.sh` is deliberately awkward. `set -e` would abort
the script the moment `dbt test` returned non-zero — correct for the
publish gate, but it would mean the single most interesting run, the
failing one, was the only one never recorded. So the exit code is captured,
the results are published either way, and the code is re-raised before
anything reaches the marts. Publishing is itself best-effort: a failure
there prints a warning and exits 0, because a dashboard missing a datapoint
is a far smaller problem than a nightly build that dies because its metrics
sink was down.

The effect is that a failed night is now visible on the dashboard and
pages a phone (see below), instead of being a non-zero exit sitting in
`build.log` waiting for somebody to think to open it. Warn-severity test
results and freshness warnings — which never failed the build, and so
previously surfaced nowhere at all — are counted in the same rows.

### GOTCHA: every number sized before 2026-09-12 was sized for the North West, not Great Britain

On 2026-09-12 the ingest bounding box went from the **north-west of England
only** to **all of mainland Great Britain** (`bus-positions.odcs.yaml`
`boundingBox`; a same-instant measurement showed 4.72x the vehicles). Rows
per day went from ~3M (Sep 7-11) to ~13M at the Sep 15-16 peak. Anything
tuned or measured before that date was measured against the wrong volume:
`memory_limit`, `MemoryMax` (ADR-0001/0002/0005), QuestDB's 8g cap, the
44GB `bus_positions` before ADR-0006, the ~65-minute nightly, the parquet
window in `dbt/data/`. Treat every pre-09-12 number as suspect and retune
against the current volume. The 8-day window also took a week to fill with
wide-box days, so a limit that held on Sep 15 could fail on Sep 19 with
nothing else changed. Whether to keep the whole-GB box is an open decision
(ROADMAP, "Known issues").

### GOTCHA: dbt reads every parquet file in `dbt/data/`, so the SSD must hold exactly the window

`stg_bods__vehicle_pings` globs `data/bus_positions_*.parquet` with no date
filter. `extract.py` re-extracts the last `EXTRACT_DAYS` (8) days, but until
2026-09-19 nothing ever deleted a day that had slid out of the window, so
the 03:00 build that night read 12 days (81.9M rows) instead of 8. The SSD
holds the 8-day window and nothing older; **SATA is the archive** ("glacier"),
not a second copy of the working set. `extract.py` now prunes parquet older
than the window, but only after a successful extract, so a failed run can
never be the thing that deletes the last good data. The parquet is derived
data and is regenerable from QuestDB, so it is pruned rather than archived.

## Archiving, not deleting

`archiver/archive.py` runs continuously in its own container, checking
once every 24 hours (`POLL_S`) for `bus_positions` partitions older than
`RETENTION_DAYS` (8, set in `.env`). QuestDB's native `SET TTL` was
considered and rejected because TTL **drops** partitions outright; this
box instead moves them to a separate cold drive intact.

The mechanism: `ALTER TABLE bus_positions DETACH PARTITION LIST '<day>'`
lifts a partition out of the live table without destroying it, then the
detached directory is `copytree`'d to `${ARCHIVE_PATH}/bus_positions/<day>`
and verified by byte size before the local copy is freed. A partition that
fails the size check is left both detached-and-local and archived rather
than deleted from either side — it costs disk space until the next run
retries, which is a far better failure mode than losing a day of data.

Before touching anything, the archiver checks for a `.this-is-vault2`
sentinel file inside `ARCHIVE_PATH`. If it's missing, the run refuses to
touch any partition rather than risk archiving onto the wrong disk if the
real archive mount isn't actually mounted there.

A crash between DETACH succeeding and the copy finishing leaves a
`<day>.detached` directory that QuestDB no longer considers live but the
archiver hasn't finished migrating. Each run checks the filesystem
directly for orphaned `.detached` directories first, before looking at
what's newly eligible, so this resumes cleanly rather than being silently
skipped.

### GOTCHA: `RENAME TABLE` keeps the physical directory name

QuestDB renames the SQL name only. After ADR-0006's cutover the live
`bus_positions` is the directory `bus_positions_v2~17`, so any
`bus_positions~*` glob or `find` matches nothing. The archiver failed
on exactly that at 06:00 on 2026-09-19 (`expected exactly one bus_positions~*
dir, found []`), and `ops/incident.py` had the same pattern. Both now ask
QuestDB: `SELECT directoryName FROM tables() WHERE table_name = 'bus_positions'`.
Never hardcode or glob a table's directory.

### GOTCHA: a restored day can jam the archiver, and it stays jammed

`ops/incident.py restore` leaves the day attached in the live table. Once it
is older than `RETENTION_DAYS` the archiver detaches it, finds
`${ARCHIVE_PATH}/bus_positions/<day>` already exists (that is where it came
from), logs `archive destination ... already exists -- stopping, needs a
human`, and stops the whole run. Every later run resumes that same orphan
and stops again, so **nothing else is archived until it is cleared**. This
happened after the 2026-07-03 restore proof: nothing archived from
2026-09-16 to 2026-09-19 and Sep 7-10 sat on the SSD. It is not fixed in
code. After finishing with a restored day, drop it from the live table once
you have confirmed the archive copy exists (`ALTER TABLE bus_positions DROP
PARTITION LIST '<day>'`; untested here). If a `<day>.detached` orphan is
already there, compare its size with the archive copy before removing
either.

## Recovering a lost day: `ops/incident.py`

`ops/incident.py` is the other half of the archive: `archive.py` moves
partitions out, this brings them back and rebuilds whatever depended on
them. It runs on the host rather than in a container because it needs both
`docker` (to reach into QuestDB's uid-10001-owned partition directory —
the same workaround used for `server.conf` edits) and this repo's
virtualenv, since it imports `extract.py` and reuses its chunked fetch
rather than reimplementing it.

```bash
ops/incident.py restore 2026-08-02             # one archived day back into QuestDB
ops/incident.py replay 2026-08-01 2026-08-03   # restore + re-extract + rebuild
```

`restore <day>` copies the archived partition into the container, chowns it
to uid 10001, and `ALTER TABLE bus_positions ATTACH PARTITION LIST '<day>'`.
It then polls `table_partitions()` for up to 10s instead of trusting the
DDL's own return, because "accepted" and "visible as attached" are not the
same moment. It is idempotent — a day already attached is a no-op, not an
error — which is what makes it safe for `replay` to call blindly.

`replay <start> <end>` is built on `restore` rather than duplicating it.
For each day in the range it restores the day if that day falls outside the
hot `RETENTION_DAYS` window, re-extracts it into `dbt/data/` through
`extract.py`'s own `fetch()`, and then covers the whole range with a single
`SKIP_EXTRACT=1 FULL_REFRESH=1 REPLAY=1 ./build.sh` at the end rather than
one rebuild per day.

Two details in there matter more than they look:

- The extract deletes `data/bus_positions_<day>.parquet` before it starts.
  That whole-day filename predates the 4-hour chunking but still matches
  the glob the chunks are read back through, so one left behind by an older
  run silently double-counts the day. `extract.py`'s own `main()` does the
  same thing for the same reason.
- Replay leans on `bus_positions`' `DEDUP UPSERT KEYS(bus_id, timestamp)`.
  Replaying a day that was only *partially* lost re-inserts rows that are
  already there; without dedup those land as duplicates instead of no-ops.

### What `REPLAY=1` does to retention

`incident.py` always sets `REPLAY=1`, which makes `build.sh` pass
`--vars '{replay: true}'`. Both incremental models
(`int_pings_segmented_to_journeys` and `int_pings_matched_to_stops`)
collapse their retention `post_hook` to an empty list when that var is
true.

Without it, a replay quietly undoes itself. Those hooks prune each model to
`max(journey_local_date)` minus the retention window, and `max` is still
today — so replaying days deliberately *older* than the window rebuilds
exactly the rows the post-hook then deletes, in the same run, with every
step reporting success. The var applies to that one run only; the next
nightly prunes as normal.

### The day you cannot get back

`replay` only helps for days that still exist in cold storage. A day lost
*inside* the hot `RETENTION_DAYS` window is not recoverable — not by this
script, and not by anything else:

- `extract_day()` re-reads the day from QuestDB, which is precisely where
  the rows are already missing.
- The archive does not have it. That is what "inside the window" means.
- BODS serves live positions only. There is no backfill API and no second
  source to go back to.

Which is why the feed-stale alert pages a phone rather than sitting on a
dashboard someone opens on Mondays. An ingest outage is not a backlog to
catch up on afterwards: every minute the ingestor is down is a minute no
tool in this repo can ever fill in. Noticing quickly is the whole
mitigation.

## The API never needs a restart

`backend/app.py` opens `wraith_bus_ingestor.duckdb` (mounted read-only from `dbt/`)
and caches its contents in memory. Rather than watching for a restart or a
signal, it polls the file's `mtime` — checked at most once a minute
(`MARTS_TTL`) — and reloads automatically the moment the nightly build's
atomic rename lands a new file. A rebuild in progress, a missing file, or a
corrupt one all degrade to `"unknown"` status and null predictions instead
of a crash; `GET /model-info` reports which of those states is currently
active and how stale the served marts are.

Practically: after `FULL_REFRESH=1 ./build.sh` completes, there is nothing
further to do — the API picks up the new marts on its own within a minute,
with no `docker compose restart` and no downtime.

That is data. A change to `backend/app.py` itself is a different thing
entirely, and easy to conflate with the above: **merging a PR does not
deploy it.** See [ADR-0004](adr/0004-api-container-has-no-deploy-automation.md)
for the full story — none of the three `.gitea/workflows/*.yml` files
rebuild `api`, so a merged, released, tagged change sits inert in the
image until someone runs, by hand, on the host:

```bash
docker compose build api
docker compose up -d --force-recreate api
docker exec ghost-web nginx -s reload
```

The `nginx -s reload` is not optional: recreating a container changes its
IP on `ghost-net`, and `ghost-web` cached the old one at its own startup
(see [Architecture](architecture.md)). Skipping it means the new code is
running but unreachable, which looks identical from the outside to the
old code still running — check with `curl` against a route the change
actually touches, not just that the container is healthy.

## Dashboards and alerting

### Reaching Grafana

Grafana is public and read-only at the site's `/grafana/` path. The nginx
`location` that proxies it lives in `ghost-site`, not here (see
[Architecture](architecture.md)); this repo only puts the container on
`ghost-net`, with no host port, exactly like `api` and `osrm`.

Anonymous access is on with the `Viewer` role, so the dashboards read
without a login. That is the load-bearing setting: the admin password gates
*editing*, not *reading*, and anything the panels can see is public by
definition. Editing and admin need a login as `admin` with
`GRAFANA_ADMIN_PASSWORD` from `.env`.

Two settings have to agree or the page comes up broken rather than erroring:
`GRAFANA_ROOT_URL` must match the nginx location *including its trailing
slash*, since Grafana builds every asset URL by appending to that string,
and `GF_SERVER_SERVE_FROM_SUB_PATH=true` must be set for a subpath to be
served at all.

Datasource, dashboards, contact point and alert rules are all provisioned
from `ops/grafana/provisioning/`, mounted read-only, with
`allowUiUpdates: false`. The files in git are the source of truth, so an
edit made in the UI does not survive — change the file and the provisioner
picks it up within 30s. All three alert rules provision, schedule and
evaluate to `health=ok` against live QuestDB — confirmed live, not just
written against the docs.

### The Grafana image is pinned to `11.6.16` — do not casually bump it

`:latest` first shipped Grafana 13's "Dynamic dashboards" (GA and on by
default since 2026-04-08): a provisioned classic-schema dashboard like
this one renders fine — layout, titles, panels all correct — but its
frontend never issues the panel query request at all. Confirmed live by
watching nginx's access log across four full page loads from a real
device: zero `/api/ds/query` calls, ever, while the exact same query
worked correctly through `curl`. Every panel showed "No data". Known
upstream issue: [grafana/grafana#122663](https://github.com/grafana/grafana/issues/122663).

`12.3.11` did **not** fix it — that version runs the same
apiserver-backed dashboard path under a `v1beta1` API instead of `v2`,
and setting `GF_FEATURE_TOGGLES_KUBERNETESDASHBOARDS=false` (plus the
three `dashboardScene*` toggles) had no effect: `/api/frontend/settings`
reported them back as `true` regardless of the setting. That toggle is
locked at General Availability, not merely defaulted on — no config
combination turns it off.

`11.6.16` predates Dynamic Dashboards existing in any form (introduced in
12.0, May 2025). Both `/apis/dashboard.grafana.app/v1beta1/...` and
`/apis/dashboard.grafana.app/v2/...` genuinely 404 on it — the code isn't
there, not just hidden. Before bumping this image, confirm those
endpoints still 404 on whatever version you're moving to; if either
responds, the dashboard will silently stop querying.

`GRAFANA_DATA_PATH` has two traps of its own. It is written by Grafana's
in-container uid 472, so it must be `chown`ed once before first boot or
Grafana exits on a permission error — the same class of problem as
QuestDB's uid 10001 next door. And it must be absolute or `./`-prefixed: a
bare name is read by Compose as a *named volume*, so Grafana would come up
empty every time with nothing obviously wrong.

`ghost-site`'s `proxy_pass` for `/grafana/` has a trap too, found the hard
way as a `net::ERR_TOO_MANY_REDIRECTS` incident: it must have **no
trailing slash and no path** (`proxy_pass http://upstream;`). A trailing
slash strips the matched `/grafana/` prefix, which `GF_SERVER_SERVE_FROM_SUB_PATH=true`
needs preserved — stripped, Grafana 301s to re-add it, nginx strips it
again, forever. See [Architecture](architecture.md).

### What's on the dashboard

`ops/grafana/provisioning/dashboards/wraith-bus.json` — "Wraith Bus
Platform", four rows:

- **Is the pipeline alive?** Feed age, buses tracked (5 min), positions
  ingested (1 h), hours since last dbt build, DQ issues on the last
  build — the five things worth glancing at first.
- **Ingest over time / Fleet right now.** Positions-per-hour and
  distinct-buses trends, busiest operators and lines in the last 15
  minutes. Operators show by real name ("Transport for London"), not the
  raw four-letter NOC code ("TFLO") — a `LEFT JOIN` against a small
  `operator_names` table, `COALESCE`d back to the raw code for anything
  unmapped so an unrecognised operator never disappears from the panel.
  That table is loaded by `dbt/publish_operator_names.py` from
  `dbt/noc_operator_names.csv`, a curated extract of the real National
  Operator Code database (`travelinedata.org.uk`) covering the operators
  this feed has actually seen, not the full ~3,300-operator UK-wide
  database. It's reference data, not build output, so nothing calls this
  script automatically — run it by hand when a new operator's code shows
  up unmapped on the dashboard.
- **Data quality.** dbt test outcomes and build duration per run (a
  callout with the delta from the previous build, not a graph — the
  build only runs nightly, so one point a day reads better as a number),
  slowest tests and any non-passing tests on the latest build.
- **Punctuality & ETA models.** System on-time % (DfT standard, see
  below), system median delay, average model fit (r2), % of ETA-model
  grains with a weak fit, average modelled speed, and a full table of
  every line with 50+ journeys ranked by delay. Backed by
  `dbt/publish_punctuality_summary.py`, which collapses the
  `line_punctuality` and `eta_models` dbt marts (otherwise stuck in
  duckdb, same problem as the DQ results) into QuestDB after every
  build. "On-time" is the DfT Traffic Commissioner standard for local
  bus services — 1 minute early to 5 minutes 59 seconds late, 95% the
  statutory target — not a number picked for this dashboard. The rollup
  is a volume-weighted approximation from grain-level medians, not a
  true per-journey percentage: no per-journey delay is stored anywhere
  in this pipeline, only the medians dbt already computes.

`slaycoach.quest/oma` 302-redirects straight to this dashboard — a
redirect rather than a proxy, since Grafana's `GF_SERVER_ROOT_URL` is
fixed to the `thomaspeoples.com` domain and serving the dashboard under a
second domain would break its own generated asset URLs.

### Where the alerts actually go

A rule fires in Grafana Alerting, Grafana's webhook contact point POSTs to
`http://ntfy:80/<topic>`, the self-hosted `ntfy` container holds that topic,
and the stock ntfy Android app — subscribed to the same topic — raises the
notification on the phone. No SMTP hop, no mail account, no third-party push
service in the middle.

Three rules are provisioned today:

- **feed stale** — nothing written to `bus_positions` for 15 minutes, which
  never happens during service hours. `NoData` counts as alerting here: a
  query that cannot run is its own outage, not a reason to stay quiet.
- **nightly build missing or failed** — no successful `dbt_build_runs` row
  in the last 26 hours. 26 rather than 24 gives the nightly job room to
  drift or run long without flapping, and one rule covers both "never ran"
  and "ran and failed" — the first being the failure that went unnoticed
  for weeks after the September repo split, because a build that never runs
  produces no output to read.
- **dbt test failures or warnings** — the most recent run's
  `tests_failed` plus `tests_warned` is above zero. Warnings count
  deliberately: a warn-severity result (a freshness check, say) exits the
  build green and publishes anyway, so before this rule existed it
  reached nothing but `build.log` — this is the alert that closes that
  gap, not just the outright failures.

### The topic name is the credential

`ntfy` runs here with no accounts and no auth. Anyone who knows the topic
name can subscribe to it, and anyone who knows it can publish to it — that
is the whole access model, so `NTFY_TOPIC` is a secret in the ordinary
sense: random, kept in `.env`, never committed, never printed to
`build.log`, never returned in an HTTP response. Grafana's POST is
server-side over `ghost-net`, so the topic never appears in anything a
browser loads.

### Rotating the topic

1. Change `NTFY_TOPIC` in `.env`.
2. `docker compose up -d --force-recreate grafana ntfy`. The value is
   injected into `grafana`'s environment and read by the alerting
   provisioner, so a restart is not enough — the container has to be
   recreated. `ntfy` carries no topic configuration of its own; it goes in
   the same command so nothing cached against the old topic outlives it.
3. `docker exec ghost-web nginx -s reload`, because recreating containers
   changes their IPs and nginx cached the old ones (see
   [Architecture](architecture.md)).
4. Re-subscribe the phone in the ntfy app, and delete the old subscription.

Nothing else stores it. `ntfy` creates topics on demand and keeps no
registry, so there is nothing server-side to pre-create or to delete — the
new topic exists the first time Grafana posts to it. Grafana picks the name
up from its environment each time it provisions the contact point, so
recreating the container is the whole update; there is no copy anywhere to
hand-edit. `$NTFY_TOPIC` interpolation in `contactpoints.yaml` is
confirmed to resolve — Grafana documents env-var interpolation for
datasource provisioning only, but reading the contact point back from
the API after a recreate shows the real topic, not the literal
`$NTFY_TOPIC` string, so the same mechanism covers the alerting
provisioner too.

The old topic keeps working for anyone who still knows it. That is the
point: a leaked topic cannot be revoked, only abandoned.

## Container lifecycle

The stack is long-running; this repo's automation does not restart,
rebuild, or otherwise touch running containers. Standard compose commands
apply directly on the host:

```bash
docker compose up -d              # start/reconcile the stack
docker compose logs -f ingestor   # tail the BODS poller
docker compose logs -f archiver   # tail the daily retention sweep
docker compose build api          # rebuild the API image after a code change
```

### GOTCHA: `docker compose down` + `up` silently breaks the public site

Not just a single-service `--force-recreate` -- a full stack bounce does
this too, and it's easier to forget precisely because nothing looks
wrong. Hit for real on 2026-09-17/18: the whole stack was stopped
deliberately (`docker compose down`) for the dbt v2 migration, then
brought back with `docker compose up -d`. Every container came back
`Up`/`healthy`. Grafana (`/grafana/d/wraith-bus/...`) came back as a
Cloudflare error; the public map (`/bus-map/`) came back with `map
error: AJAXError: Bad Gateway (502): .../tiles/great-britain/0/0/0`.
Both containers were fine -- confirmed with `docker exec ghost-tiles`/
`ghost-grafana` directly. The break was entirely in `ghost-web`.

**Cause** (see [Architecture](architecture.md)): `ghost-web` resolves
`ghost-tiles`/`ghost-grafana`/`ghost-backend`/`ghost-ntfy` by container
name to an IP once, at its own nginx startup, and never again. `down` +
`up` gives every container a new IP on `ghost-net`. `ghost-web` itself
was untouched (still `Up`, uptime unaffected by the other containers
restarting), so it just kept sending traffic to the old, now-dead IPs --
indefinitely, until told to look again.

**Not just recreate -- plain `docker stop` + `start` on an existing
container does this too**, confirmed directly 2026-09-18: no recreate,
same container ID, still a new IP. This matters because
`wraith-bus-build.service` does exactly `docker stop` then `docker
start` on `ghost-osrm`/`ghost-backend`/`ghost-tiles` every single night
(see `ops/systemd/wraith-bus-build.service`) -- so this bug had been
silently breaking `/tiles/` and `/bus-map/` after every nightly build,
not just after manual full-stack bounces. Fixed at the source 2026-09-18:
the unit now carries its own `ExecStopPost=-docker exec ghost-web nginx
-s reload` rather than depending on a human remembering.

**Fix, every time this stack (or any subset of it) is stopped/started
or recreated by hand:**

```bash
docker exec ghost-web nginx -s reload
```

Cheap, instant, zero downtime of its own -- but there's no automation
that runs it for a manual action, and no health check that catches its
absence, because every container this stack owns reports genuinely
healthy the whole time. The only signal is the public site itself
breaking. **After any manual `docker compose down`/`up`, `docker
stop`/`start`, or `--force-recreate` on this stack, check `/grafana/`,
`/bus-map/`, and `/tiles/...` from outside the host -- not just `docker
compose ps`.**
