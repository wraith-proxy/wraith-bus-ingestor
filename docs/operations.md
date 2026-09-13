# Operations

## The nightly rebuild

`dbt/build.sh` runs from a host cron entry at **03:00** every night — this
is a host crontab, not a container, since it needs the `dbt/.venv`
environment and to run at low priority alongside the live stack:

```
0 3 * * * cd /path/to/wraith-bus-ingestor/dbt && ./build.sh >> /var/log/ghost-bus-build.log 2>&1
```

A run does four things in order: extract the last `EXTRACT_DAYS` (default
22) of `bus_positions` out of QuestDB into `data/*.parquet`, `dbt run`
every model into a scratch warehouse, `dbt test` the result, and — only if
the tests pass — publish the five marts into `ghost_bus.duckdb`, the file
the API reads. A failed test run leaves the previous night's marts serving
rather than replacing them with something known to be wrong.

The whole thing runs under `nice -n 15` (and `ionice -c3` where available):
DuckDB will happily take every core on the box, and unconstrained it drove
load past 10 on the same 6-core host that also runs QuestDB, the ingestor
and the API — pushing the API's p50 from 1.3s to 15.6s, long enough for
nginx to start returning 504s. A slower rebuild is an acceptable cost; the
site falling over during it is not.

### The three-file handoff

`build.sh` never lets dbt build the file Flask has open. DuckDB holds an
exclusive lock for the whole run, so an earlier version that built in
place made every read-only open from the API fail with `Conflicting lock
is held`, greying out every bus on the map for the run's duration.

Instead:

1. `dbt run` builds into **`ghost_bus_build.duckdb`**, a persistent
   scratch warehouse (see below for why it persists).
2. Once `dbt test` passes, the five marts are copied into a fresh
   **`ghost_bus_staged.duckdb`**.
3. `ghost_bus_staged.duckdb` is `mv`'d over **`ghost_bus.duckdb`** — atomic
   within a filesystem, so the API sees either the whole old file or the
   whole new one, never a lock or a partial write.

### Why the scratch warehouse persists across runs

`ghost_bus_build.duckdb` used to be wiped before every run. Two of the
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

Wipes `ghost_bus_build.duckdb` and passes `--full-refresh` to `dbt run`,
forcing both incremental models to rebuild from nothing. Use this after a
schema change to either model, or if incremental drift is suspected — not
routinely, since it's the slow path the persistence above exists to avoid.

Other flags:

```bash
SKIP_EXTRACT=1 ./build.sh   # reuse the parquet already in data/, skip the QuestDB pull
```

## Archiving, not deleting

`archiver/archive.py` runs continuously in its own container, checking
once every 24 hours (`POLL_S`) for `bus_positions` partitions older than
`RETENTION_DAYS` (22, set in `.env`). QuestDB's native `SET TTL` was
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

## The API never needs a restart

`backend/app.py` opens `ghost_bus.duckdb` (mounted read-only from `dbt/`)
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
