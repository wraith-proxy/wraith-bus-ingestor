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

### Proving a build end to end: `ops/verify_build.sh`

```bash
sudo ops/verify_build.sh --detach   # recommended: prompt returns at once, survives the terminal closing
sudo ops/verify_build.sh            # foreground: blocks ~2.5-3 hours; a dropped SSH session aborts it
ops/verify_build.sh --check         # preflight only (no sudo, stops nothing)
ops/verify_build.sh --dry-run       # the whole flow with fake builds, in seconds
tail -f ops/logs/verify-*/verify.log   # watch a detached run (the newest directory)
```

A clean isolated `dbt run` proved little on 2026-09-18/19: the failures only
showed under the real nightly conditions. This runs **Run 1**, `SKIP_EXTRACT=1
./build.sh` as your user under `time -v` (peak RSS of the biggest process, the
DuckDB one that hits `memory_limit`), then **Run 2**, `systemctl start
wraith-bus-build.service` (the production path: extract, build, test, publish,
under the unit's `MemoryMax`, sampling peak RSS from its cgroup). It stops the
containers the unit stops plus ollama/immich (all restored by a trap however it
exits), reclaims swap first, and afterwards checks the marts were republished,
the build was recorded as a success, the public site answers, and the API's
`marts_state` is `ok`. Everything goes to one log, `ops/logs/verify-<stamp>/
verify.log`, which is also tailed live in the terminal. The public site is dark
for the duration (backend, tiles, grafana stopped). Exit status 0 means safe to
push.

Run 1 uses the same minimal environment systemd gives the unit (`env -i`, no
`~/.local/bin` on PATH, not root's `HOME`), on purpose.

### The build window stops what the build does not need, and restarts it

`wraith-bus-build.service` stops `ghost-osrm`, `ghost-backend`, `ghost-tiles` and
`ghost-grafana` before the build and starts them after (the public site is dark
for the build's ~1h+), and, since 2026-09-19, **ollama and immich** too (~4GB,
only the owner uses them). ollama/immich go through `ops/build_window.sh`, not a
plain `docker stop`/`start` pair, so only containers that run actually stopped
come back: one you had deliberately left off stays off. It records each
container before stopping it, restarts them in reverse order (postgres/redis
before `immich_server`), and refuses to stop anything if it cannot write its
state file (`/run/wraith-bus-build/`, the unit's `RuntimeDirectory`), because
without a record nothing could bring them back. The restart runs when the build
fails too (`ExecStopPost`), and both lines are `-`-prefixed so this can never
fail a build. `ops/verify_build.sh` does the same stopping itself for its
manual Run 1. Changing the unit file needs `sudo cp` to `/etc/systemd/system/`
and `daemon-reload`.

### A failed nightly now alerts within ~10 minutes

`wraith-bus-build.service` runs `ops/record_build_failure.sh` as an `ExecStopPost`.
If the unit did not end in `success` (an OOM in `dbt run`, an extract failure, a
kill), it writes a `failed` row to `dbt_build_runs`, and the existing Grafana rule
"nightly build missing or failed" (`last(result) != 'success'`) notifies ntfy.
Before this, a run that died before dbt published its results recorded nothing:
on 2026-09-20 the 03:00 build failed with a DuckDB OOM and everything looked fine,
because the API kept serving the previous good marts and the rule only fires 26h
after the last success. The next successful build writes its own `success` row,
which resolves the alert. A hard crash of the host itself cannot record anything;
the 26h rule is still the backstop for that.

### GOTCHA: `memory_limit` is a coin flip at the edge, and RSS runs above it

DuckDB's `memory_limit` (`dbt/profiles.yml`) bounds only its own buffer manager.
On this stack the process RSS ran ~4.7GB **above** it (8.69GB RSS at a 4GB limit),
so the limit and the systemd `MemoryMax` (13000M) must be sized together, not
separately. 4GB passed `int_pings_matched_to_stops` twice on 2026-09-19 (under
`ops/verify_build.sh`) and failed it at 03:00 the next morning, so "it passed"
twice was not proof. It is 6GB since 2026-09-20 (~11GB peak RSS, under the cap);
8GB would need `MemoryMax` raised first. Extra host RAM does not raise this limit.

### GOTCHA: the API holds every mart row in Python memory, and the marts outgrew its limit

`backend/app.py` loads `eta_models`, `localities`, `line_localities` and
`line_stops` (~2.6M rows) into Python dicts on the first request after a
publish. Since the whole-GB bounding box (2026-09-12) that needs 2.46GiB steady
after a fresh load and **4.29GiB peak on an in-place reload** (old and new
copies coexist), settling at 3.44GiB. Its container limit was 1.5g, so on
2026-09-19 the first request after a good publish took it from 93MB to the limit
in ~6s, the kernel OOM-killed it, and Docker restarted it into the same
death: `marts_state: ok` never came, the public `/api` returned nothing, and
predictions stayed dead even though the build had succeeded. The limit is 6g
now (`docker-compose.yml`). Symptoms to recognise: `journalctl -k | grep "Killed
process"` naming a `python` in a `ghost-backend` docker scope, `docker inspect
ghost-backend` with a climbing `RestartCount`, and the API log showing only
repeated startup banners. Nothing alerts on this; an API-state alert is an open
gap. Same root cause as the memory GOTCHA above: every capacity number sized
before 2026-09-12 was sized for the North West.


### GOTCHA: systemd's PATH does not include `~/.local/bin`

`uv` lives there. On an interactive shell it is found; under
`wraith-bus-build.service` it was not, so the contract gate (`uv run
contracts/validate.py`) would have failed after an hour of build and before
publishing. Nothing had ever run a systemd build that far. `build.sh` now
resolves `uv` itself (PATH, then `$HOME/.local/bin`) and exits in seconds if it
cannot find it.

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
once every 24 hours (`POLL_S`) for `bus_positions` days older than
`RETENTION_DAYS` (8, set in `.env`). QuestDB's native `SET TTL` was
considered and rejected because TTL **drops** partitions outright; this
box exports them to a separate cold drive first (ADR-0007).

**The archive is Parquet.** For each eligible day the archiver:

1. streams QuestDB's own Parquet export (`/exp?fmt=parquet`, zstd level 3) to
   `${ARCHIVE_PATH}/bus_positions_parquet/<day>.parquet.part`;
2. reads that file back with DuckDB and checks it against QuestDB: the row
   count must equal `count()` for that day, and the first and last timestamps
   must fall inside the day;
3. only then renames it into place, hands it to the archive owner
   (`ARCHIVE_OWNER`, default `1000:1000`), and `ALTER TABLE bus_positions DROP
   PARTITION`s the live copy.

A day is never dropped before its file has been read back and matches. If
verification fails the file is discarded and the live copy stays. Measured on
a 34.1M-row day: 3.0GB of QuestDB partition became 0.69GB of Parquet in 25s
(the QuestDB default codec gave 1.22GB; zstd level 9 saved only 3.6% more for
2.3x the time). QuestDB peaked at ~4GB of its 8GB doing it.

Each file is self-describing: real strings, its own schema, no dependency on
`bus_positions` continuing to exist. So the archive can be **queried in place**
without restoring anything:

```bash
duckdb -c "SELECT line, count(*) FROM read_parquet(
  '/mnt/archive/wraith-bus-ingestor-archive/bus_positions_parquet/*.parquet',
  union_by_name = true) GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
```

`union_by_name` matters: the 63 numeric-only days (below) have `bus_id_key` /
`line_key` instead of `bus_id` / `line`, and read as NULL for those columns.

Before touching anything, the archiver checks for a `.this-is-vault2`
sentinel file inside `ARCHIVE_PATH`. If it's missing, the run refuses to
touch any partition rather than risk archiving onto the wrong disk if the
real archive mount isn't actually mounted there. An export interrupted
mid-write leaves a `.part` file, which the next run deletes; a half-written
file can never look like a finished archive.

Every run records itself in QuestDB (`archiver_runs`: `ts, status, archived,
skipped, detail`; status `ok` / `attention` / `error`). Grafana can see QuestDB
but not the archive drive, so that table is what the two archiver alert rules
read, and they reach your phone through the same ntfy path as the others (every rule
notifies in plain text: the rule's name as the title, `ALERT: <the rule's summary>`
as the body while it fires, and `RESOLVED` when it clears. ntfy renders that from
Grafana's JSON with inline templates set in `contactpoints.yaml`; without them the
phone shows the raw JSON and the topic name, which is the credential, as the
title):

- **archiver has not run cleanly**: no `ok` row in 36h. The dead-man switch:
  container down, or every run failing. This is the alert that would have caught
  the 2026-09-16 to 09-19 jam, when nothing was archived and nobody was told.
- **archiver needs attention**: the latest run was `attention` (a day was left
  in place on purpose) or `error`. It clears when a later run is clean;
  `docker restart ghost-archiver` runs one immediately.

### The 63 numeric-only days

The archive was raw QuestDB partition directories until ADR-0007. Those were
converted and then **deleted on 2026-09-19** (46.7GB; a manifest is in
`raw_partitions_deleted_2026-09-19.txt` on the archive drive). Parquet is the
only archive now, 68 files:

- 5 days (2026-09-07 to 09-11) were archived after the schema migration, so they
  decoded fully and were converted losslessly (restored, then exported).
- 63 days (2026-07-03 to 2026-09-06) had their `bus_id` / `line` symbol maps
  destroyed (ADR-0006). `ops/salvage_legacy_archive.py` wrote what still exists:
  `timestamp`, `lat`, `lon`, `bearing` and the raw `bus_id_key` / `line_key`
  integers, with the file's key/value metadata (`wraith.layout =
  numeric-salvage`) saying so. Padding rows were trimmed and every assumption
  asserted first; an independent recount matched all 63 before the raw copy was
  deleted. That script is kept as provenance and cannot be re-run: its source is
  gone.

### GOTCHA: `RENAME TABLE` keeps the physical directory name

QuestDB renames the SQL name only. After ADR-0006's cutover the live
`bus_positions` is the directory `bus_positions_v2~17`, so any
`bus_positions~*` glob or `find` matches nothing. The archiver failed
on exactly that at 06:00 on 2026-09-19 (`expected exactly one bus_positions~*
dir, found []`), and `ops/incident.py` had the same pattern. Both now ask
QuestDB: `SELECT directoryName FROM tables() WHERE table_name = 'bus_positions'`.
Never hardcode or glob a table's directory.

### GOTCHA: raw QuestDB partitions are not self-contained (why the archive is Parquet)

A DETACHed partition holds SYMBOL columns (`bus_id`, `line`, ...) as integer
keys into the **source table's** symbol maps, and those map files are not in
the archive. QuestDB also re-attaches a partition only into the table it was
detached from (`Detached partition metadata [table_id] is not compatible with
current table metadata`). So a raw archive is only as good as the table it came
from: dropping or recreating that table strands every archived day, which
happened to 63 days in ADR-0006's migration. Parquet has neither problem, and
is the archive format since ADR-0007. The raw directories were deleted once
the Parquet copies were verified.

### GOTCHA: a restored day comes back through the archiver

`ops/incident.py restore` puts the day back in the live table. Once it is older
than `RETENTION_DAYS` the archiver picks it up again and finds
`bus_positions_parquet/<day>.parquet` already exists (that is where it came
from). Until 2026-09-19 the old archiver logged `archive destination ... already
exists -- stopping, needs a human` and stopped the whole run, and every later
run stopped again, so **nothing else was archived** from 2026-09-16 to
2026-09-19.

It now never rewrites an existing file and never stops for it. It reads the file
back: if its row count equals the live day it drops the live copy ("already
archived (N rows verified)"); if not, it logs a `WARNING`, leaves the live day
in place and skips it, the run status becomes `attention`, and the alert fires.
Nothing is dropped on a guess about which side is right. The normal outcome is
the first (verified on 2026-09-11, twice). Any other failure (an export that
fails verification, a failed DROP, a missing sentinel) stops the run.

## Recovering a lost day: `ops/incident.py`

`ops/incident.py` is the other half of the archive: `archive.py` exports days
out, this brings them back and rebuilds whatever depended on them. It runs on
the host rather than in a container because it needs both `docker` (to copy the
file into QuestDB's import directory, which is owned by uid 10001) and this
repo's virtualenv, since it imports `extract.py` and reuses its chunked fetch
rather than reimplementing it.

```bash
ops/incident.py restore 2026-09-08             # one archived day back into QuestDB
ops/incident.py replay 2026-09-01 2026-09-03   # restore + re-extract + rebuild
```

`restore <day>` reads the day's Parquet file, copies it into QuestDB's `import`
directory, and `INSERT`s it back through `read_parquet()`. It then polls until
QuestDB's row count for the day equals the file's, because the insert is applied
asynchronously, and removes the copy. It is idempotent: a day already in the
table is a no-op, not an error, which is what makes it safe for `replay` to call
blindly. It **refuses the 63 numeric-only days** (2026-07-03 to 2026-09-06),
which have no `bus_id` to load; read those in place with DuckDB. Fidelity is
checked, not assumed: on 2026-09-11 the restored partition matched its file on
row count, distinct buses, distinct lines and the lat/lon/bearing sums.

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
