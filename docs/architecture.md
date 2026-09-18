# Architecture

## Services

`docker-compose.yml` defines eight services, joining `ghost-net` as an
**external** network owned by `ghost-site`'s own compose file:

| Service | Image / build | Role |
|---|---|---|
| `db` | `questdb/questdb:latest` | Time-series store for raw vehicle positions, written over ILP |
| `api` | `./backend` | Flask API: live buses, search, route geometry, ETA predictions |
| `osrm` | `osrm/osrm-backend` | Self-hosted road router (car profile), used to snap route lines to real roads |
| `archiver` | `./archiver` | Moves old `bus_positions` partitions off the hot disk, on a schedule, not on request |
| `ingestor` | `./ingestor` | Polls the BODS SIRI-VM feed every 60s and writes it into QuestDB |
| `tiles` | `ghcr.io/maplibre/martin` | Serves PMTiles basemap tiles to the bus map in `ghost-site` |
| `grafana` | `grafana/grafana:11.6.16` | Dashboards over QuestDB, plus the alert rules that fire into `ntfy` |
| `ntfy` | `binwiederhier/ntfy` | Push delivery for those alerts, to the ntfy app on the phone |

None of these publish a host port except `db` and `tiles` (configurable in
`.env`, matching the ports `ghost-site`'s `web` proxies through). `api`,
`osrm`, `archiver`, `ingestor`, `grafana` and `ntfy` are reachable only from
inside `ghost-net`.

`grafana` and `ntfy` are the interesting case there: both are reached from a
browser or a phone, and neither publishes a port anyway. `ghost-site`'s
nginx proxies to them by container name over `ghost-net`, exactly as it
already does for `api` and `osrm` — so they follow the pattern rather than
inventing a second one. Publishing a port would be strictly worse: an
unproxied second way in, skipping whatever the nginx `location` does in
front of it, on a service that is deliberately readable without a login.

`api` mounts the whole `dbt/` directory read-only at `/models` rather than
just the `.duckdb` file — bind-mounting a file that doesn't exist yet makes
Docker create a directory in its place, which would break the first-ever
start before dbt has run.

`archiver` runs as root and mounts the QuestDB data directory directly,
because the partition directories on disk are owned by QuestDB's
in-container uid, which the host user can't write to.

`ingestor` pins explicit DNS servers (Cloudflare's own resolver and
`1.1.1.1`) rather than relying on the Docker embedded DNS, since it needs to
resolve `data.bus-data.dft.gov.uk` reliably from inside the container.

`grafana`'s image is pinned to `11.6.16`, not `:latest` like the rest of
this file's images -- the one deliberate exception. `:latest` picked up
Grafana 13's "Dynamic dashboards" (GA and on by default since
2026-04-08): a provisioned classic-schema dashboard renders correctly but
its frontend never issues the panel query at all, silently. `12.3.11`
turned out to run the same apiserver-backed dashboard path under a
`v1beta1` API instead of `v2`, and explicitly disabling it via
`GF_FEATURE_TOGGLES_KUBERNETESDASHBOARDS=false` had no effect -- Grafana
reported the toggle back as `true` regardless, meaning it is locked at
General Availability, not merely defaulted on. `11.6.16` predates the
whole subsystem: both `/apis/dashboard.grafana.app/v1beta1/` and `/v2/`
genuinely 404 on it. Before ever bumping this image, confirm those
endpoints still 404 on the target version.

## The seam with `ghost-site`: `ghost-net`

This repo was split out of `ghost-site` (which now holds only `www/`,
`nginx/` and the `web`/`tunnel` services). `ghost-net` is a bridge network
**owned by `ghost-site`'s compose file** (`name: ghost-net, driver: bridge`);
this repo's compose declares it `external: true` and joins it. Practically:

- `ghost-site` must be brought up **before** this repo on a cold boot, so
  `ghost-net` already exists.
- `web`'s nginx proxies `/api/` and `/tiles/` to `${UPSTREAM_API}` /
  `${UPSTREAM_TILES}` (container-name:port over `ghost-net`) — this works
  unchanged across the repo boundary as long as both stacks are on the same
  network.
- Two further `location` blocks, `/grafana/` and `/ntfy/`, were added for
  this stack's newest containers — and they live in **`ghost-site`'s repo,
  not this one**. Nothing here configures nginx; grepping this repo for
  `/grafana/` turns up `GRAFANA_ROOT_URL`, the value the location has to
  agree with, never the location itself. (That agreement matters: Grafana
  appends to its root URL to build every asset path, and needs
  `GF_SERVER_SERVE_FROM_SUB_PATH=true` to serve a subpath at all.) A
  second thing has to agree too, learned from a real
  `net::ERR_TOO_MANY_REDIRECTS` incident: `ghost-site`'s `proxy_pass` for
  that location must have **no trailing slash and no path** --
  `proxy_pass http://upstream;`, not `http://upstream/;`. The trailing
  slash strips the matched `/grafana/` prefix before forwarding, and
  `GF_SERVER_SERVE_FROM_SUB_PATH=true` means Grafana expects that prefix
  preserved on every request; stripped, it 301s to re-add it, nginx
  strips it again on the retry, and the two loop forever.
- There is no `depends_on` across the boundary (compose can't express that
  across projects); `web` 502s until this stack's containers are up, same
  as after any reboot.

## Grafana reads QuestDB directly

QuestDB already speaks the Postgres wire protocol on 8812, and Grafana
already ships a Postgres datasource, so
`ops/grafana/provisioning/datasources/questdb.yaml` is `type: postgres`
pointed at `db:8812` — the **built-in** datasource, not the QuestDB plugin.
Nothing to install, nothing to keep compatible across a Grafana upgrade, and
a panel is a plain `select` against the live table rather than a second
pipeline to build and keep in sync. Its credentials are QuestDB's shipped pg
defaults (`admin`/`quest`); `server.conf` is not tracked in this repo, so if
those were ever changed on the host, that YAML file is what has to follow.

The same reasoning decides where dbt's own results are stored.
`dbt/publish_run_results.py` parses `dbt/target/run_results.json` after
every `dbt test` and writes `dbt_test_results` (a row per test) and
`dbt_build_runs` (a row per run) into QuestDB over ILP. The duckdb marts
would be the obvious home for data *about* the marts, but DuckDB is an
embedded file with no wire protocol for Grafana to connect to — and the
moment a dashboard would most want to read it is precisely when `build.sh`
holds the exclusive lock on it. QuestDB is already the ingest sink, already
on `ghost-net`, and already the datasource, so build health plots next to
feed health with no new storage to invent.

`dbt/publish_punctuality_summary.py` follows the same pattern for
`line_punctuality` and `eta_models` -- also real dbt marts, also stuck in
duckdb -- writing `line_punctuality_summary` (one row per line: journeys,
on-time journeys, volume-weighted median/p90 delay) and
`eta_model_summary` (one system-wide row: average r2, % of grains with a
weak fit, average modeled speed) after every publish. Both are collapsed
from a finer (line, direction, day_type, hour_band) grain down to
something a dashboard panel can plot; "on-time" is the DfT Traffic
Commissioner standard (1 minute early to 5 minutes 59 seconds late), not
a number invented for this dashboard.

Alert delivery runs the other way down the same network: Grafana's webhook
contact point POSTs server-side to `http://ntfy:80/<topic>`, so the topic
name — which is the only credential `ntfy` has — never reaches a browser.
The only other thing that knows it is the phone subscribed to it.

## This repo owns its own data volumes

`questdb_data/` (19G), `maps/` (2.1G) and `osrm_data/` (8.3G) all live in
this repo's own directory. They were left behind in `ghost-site/` when the
code was split out — moving multi-GB volumes out from under a running
QuestDB and OSRM was a bigger risk than the code split needed to take on —
and were physically migrated here on 2026-09-14 in a 3m33s maintenance
window. Same filesystem, so each move was a single atomic `rename()` that
preserved QuestDB's uid 10001 ownership; verified afterwards by an exact
row-count match over the pre-migration time range (279,722,130 rows) and by
confirming all 20 partitions were still attached.

`docker-compose.yml` still reaches them through `${QUESTDB_DATA_PATH}`,
`${MAPS_PATH}` and `${OSRM_DATA_PATH}` in `.env` rather than hardcoding
paths — those values are now absolute paths inside this repo. They stay
env-indirected deliberately: `questdb_data/` is written by a container as
uid 10001, so a `docker compose` run from another checkout would otherwise
silently target the wrong tree.

One operational note learned the hard way: `ghost-web` (nginx, in
`ghost-site`) resolves this stack's container names to IPs once at startup.
Recreating these containers changes those IPs, so a
`docker exec ghost-web nginx -s reload` is needed afterwards or `/tiles/`
and `/api/` 502 while the containers themselves are perfectly healthy. This
applies unchanged to `/grafana/` and `/ntfy/` — and it bites there routinely,
because rotating `NTFY_TOPIC` is done by recreating exactly those two
containers. **Applies to a full `docker compose down` + `up` just as much
as a single `--force-recreate`** — every container in the stack gets a new
IP either way, and `ghost-web` itself typically isn't touched, so it stays
`Up` pointing at dead IPs with no health check anywhere catching it. See
[Operations § Container lifecycle](operations.md#gotcha-docker-compose-down--up-silently-breaks-the-public-site)
for the concrete 2026-09-17/18 incident and the check to run after any
stack-wide restart.

## Data contracts and ontology

`contracts/` holds two Open Data Contract Standard (ODCS) specs — one at the
ingest boundary, one at the marts boundary — plus a Foundry-style ontology
layered over both. See [Contracts](contracts.md).
