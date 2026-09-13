# Architecture

## Services

`docker-compose.yml` defines six services, joining `ghost-net` as an
**external** network owned by `ghost-site`'s own compose file:

| Service | Image / build | Role |
|---|---|---|
| `db` | `questdb/questdb:latest` | Time-series store for raw vehicle positions, written over ILP |
| `api` | `./backend` | Flask API: live buses, search, route geometry, ETA predictions |
| `osrm` | `osrm/osrm-backend` | Self-hosted road router (car profile), used to snap route lines to real roads |
| `archiver` | `./archiver` | Moves old `bus_positions` partitions off the hot disk, on a schedule, not on request |
| `ingestor` | `./ingestor` | Polls the BODS SIRI-VM feed every 60s and writes it into QuestDB |
| `tiles` | `ghcr.io/maplibre/martin` | Serves PMTiles basemap tiles to the bus map in `ghost-site` |

None of these publish a host port except `db` and `tiles` (configurable in
`.env`, matching the ports `ghost-site`'s `web` proxies through). `api`,
`osrm`, `archiver` and `ingestor` are reachable only from inside `ghost-net`.

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
- There is no `depends_on` across the boundary (compose can't express that
  across projects); `web` 502s until this stack's containers are up, same
  as after any reboot.

## A known compromise: shared data volumes are absolute paths, for now

`questdb_data/`, `maps/` and `osrm_data/` are large, live, and were not
physically moved when this repo was split out — moving multi-GB volumes out
from under a running QuestDB and OSRM was a bigger, separate risk than the
code split itself needed to take on. `docker-compose.yml` here references
them by **absolute path back into `ghost-site`'s directory** on the host,
not by a relative path within this repo. Untangling that fully (physically
migrating the data, or moving it to a location neither repo's directory
implies ownership of) is a deliberate follow-up, not an oversight — it needs
its own maintenance window with the ingestor and archiver stopped.

## Data contracts and ontology

`contracts/` holds two Open Data Contract Standard (ODCS) specs — one at the
ingest boundary, one at the marts boundary — plus a Foundry-style ontology
layered over both. See [Contracts](contracts.md).
