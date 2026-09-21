[![Documentation](https://img.shields.io/badge/docs-live-brightgreen)](https://www.thomaspeoples.com/gitea-repos/wraith-bus-ingestor/)
![Coverage](https://www.thomaspeoples.com/gitea-repos/wraith-bus-ingestor/coverage.svg)

# 👻 wraith-bus-ingestor
### *Live UK Bus Tracking for the Ghost Stack*

**wraith-bus-ingestor** is a live UK bus tracking data platform, running
end to end on a home NAS: a BODS SIRI-VM feed polled every 60 seconds,
landed in QuestDB, turned into DuckDB marts by a nightly dbt build, and
served by a Flask API. Extracted out of `ghost-site` (which now holds only
the nginx front door and the static sites) into its own repo, with full
history preserved.

The map that consumes this API — `www/slaycoach/bus-map/` — lives in
`ghost-site`; the two repos share a Docker network (`ghost-net`), not a
directory.

---

## 🌐 The Data Flow

```
BODS SIRI-VM feed (every 60s, mainland GB bbox)
  ↓ [ingestor container]
QuestDB (time-series, ILP)
  ├─ Live table: bus_positions (append-only, partitioned by day)
  └─ Retention: 8 days hot on SSD, older days exported to Parquet on a separate drive
  ↓ [dbt/build.sh, nightly 03:00 via systemd timer]
dbt models (DuckDB)
  ├─ Staging: one model per source system (bods/, naptan/), views
  ├─ Intermediate: journey cuts, direction inference, remaining distance
  └─ Marts: three frozen tables (eta_models, line_punctuality, localities)
  ↓
Flask API (read-only handle to wraith_bus_ingestor.duckdb, plus the GTFS timetable
           gtfs.duckdb -- routes, next stops and line search come from the published timetable)
  ├─ GET /buses        — live vehicle positions as GeoJSON, < 10 min old
  ├─ GET /bus/<bus_id> — one vehicle: position, model grain used, predicted arrival
  ├─ GET /route        — stop sequence, geometry and live buses for one line
  ├─ GET /search       — locality search
  └─ GET /model-info   — marts freshness, model coverage, degraded-state note
  ↓
MapLibre map on slaycoach.quest/bus-map/ (in ghost-site)
```

### Why the build matters

The dbt build is split across three files by design:

- **`wraith_bus_ingestor_build.duckdb`** — persistent incremental warehouse. The
  one expensive model (`int_pings_segmented_to_journeys`) is incremental; it
  takes the fast path only when this file already exists. Deleted before each
  run, it silently became "full rebuild every time" (about an hour).
  Persisting it across runs is what makes the nightly a matter of minutes
  ([ADR-0009](docs/adr/0009-fast-nightly-build.md) has the measurements).
- **`wraith_bus_ingestor_staged.duckdb`** — fresh per-run throwaway where the three
  marts are copied after `dbt test` and the contract gate both pass.
- **`wraith_bus_ingestor.duckdb`** — the published file the API opens read-only. Only
  `mv`d into place after both gates pass, so a bad build leaves the
  previous good marts serving instead of replacing them with something
  known to be wrong.

DuckDB holds an exclusive lock for the entire build. Moving the served file
avoids `Conflicting lock` errors and every bus on the map going grey while
the rebuild runs.

The build also runs at low priority (`nice -n 15`) because DuckDB will
steal every core it can get. Unconstrained, it drove load past 10 on a
6-core box and broke the API's response times.

### The ETA Model

One regression per `(operator, line, direction, day_type, hour_band)` grain:

```
remaining_secs = intercept_s + slope_s_per_km × remaining_km
```

The slope is the service's pace (seconds per km); invert it to get mean
speed. Fitted per-grain because the 192 outbound runs 14.7 km/h in the
morning peak and 19.2 km/h at night; pooling would predict neither.

Thin grains fall back through eleven progressively coarser rungs — drop
band, drop day pattern, drop direction, drop line, then repeat the whole
chain without operator, ending at the network average. Every fallback is
refitted from the source data at that width, not averaged from finer rows.

See [`dbt/README.md`](dbt/README.md) for the hard parts (direction inference
from bearing, detecting journeys cut mid-stream, stop sequence limitations,
stale distance chords, etc.).

## 📜 Data Contracts and Ontology

The boundary between the ingestor and everything downstream is documented
in `contracts/bus-positions.odcs.yaml` — an Open Data Contract Standard
spec that names what the SIRI-VM feed guarantees (and deliberately does
not — stale records are republished, bus IDs are not globally unique,
aimed times may be stale, etc.).

The marts contract is in `contracts/bus-marts.odcs.yaml`. The ontology in
`contracts/wraith-bus-ingestor.ontology.yaml` names the semantic types (`AtcoCode`,
`ServiceNumber`, `Direction`, `HourBand`, etc.) and proves it has not
drifted from the data. Run `uv run python contracts/validate.py` to check.

## 🚀 Quickstart

```bash
git clone https://git.thomaspeoples.com/thomaspeoples/wraith-bus-ingestor.git
cd wraith-bus-ingestor
./bin/setup.sh              # uv sync + pre-commit install

# ghost-net must already exist -- bring ghost-site up first
docker compose up -d

docker logs -f ingestor     # polling BODS
docker logs -f archiver     # runs once daily, exports old days to Parquet
docker logs -f api          # Flask API startup

# Rebuild the dbt warehouse (~5 min once the incremental warehouse is warm,
# runs nightly at 03:00 via host systemd timer -- see docs/operations.md)
cd dbt
./build.sh                  # extract from QuestDB, build, test, validate contracts, publish

SKIP_EXTRACT=1 ./build.sh   # reuse the parquet already in data/, skip the QuestDB pull
FULL_REFRESH=1 ./build.sh   # rebuild from scratch (schema change, suspected drift)
```

### Publishing dbt docs

`docs/dbt/index.html` is generated locally, against real live data, and
committed -- it is NOT built in CI. The runner has neither QuestDB nor the
NaPTAN register, so its own catalog would be sparse; CI just publishes
whatever's already committed and only falls back to generating a
placeholder if the file is missing entirely.

After a real dbt build, whenever you want the published catalog stats
refreshed:

```bash
uv run poe docs-dbt        # regenerates docs/dbt/index.html from dbt/target/
git add docs/dbt/index.html
git commit
```

## 🗂️ Layout

```
.
├── docker-compose.yml   — services: db (QuestDB), api, osrm, archiver, ingestor, tiles
│                          joins ghost-net as external -- see docs/architecture.md
├── backend/             — Flask API
├── dbt/                 — data warehouse (staging/bods, staging/naptan, intermediate, marts)
├── ingestor/            — BODS SIRI-VM → QuestDB poller
├── archiver/            — retention: export old days to Parquet on the archive drive, then drop
├── contracts/           — ODCS data contracts + ontology
│
│                        untracked, 29GB, written by the containers:
├── questdb_data/        — live QuestDB store (19G, owned by uid 10001)
├── maps/                — basemap pmtiles served by tiles (2.1G)
└── osrm_data/           — OSM extract + built routing graph (8.3G, + build.sh)
```

## 🧩 The Hard Decisions Baked In

**Days are archived, not deleted.** QuestDB's native `SET TTL` drops
data outright; this keeps 8 days hot on fast SSD (`RETENTION_DAYS`, cut
from 22 on 2026-09-14 — see `docs/ROADMAP.md` 1.0.0) and exports older
days to Parquet on a separate drive, verified row for row before the live
copy is dropped (`docs/adr/0007`). The archive is self-describing and can be
queried in place with DuckDB.

**Direction is inferred.** The feed started sending real `DirectionRef` on
2026-09-12, but historical data is NULL. `int_journeys_shaped_and_directed`
infers it from compass bearing on linear routes (split on the line's modal
bearing) and from signed area on circular routes (shoelace formula, since a
loop's bearing is noise). It is a heuristic, it is labelled as one, and it
will retire itself as new data accumulates.

**Journeys are cut by speed and movement, not time gaps.** Bus engines keep
sending position pings while parked at the terminus every three minutes, so
a "20 minutes with no pings" rule never fires.
`int_pings_segmented_to_journeys` breaks on 120 km/h crossings (physical
impossibility — a bus cannot do that but a bus_id collision can) and on
layovers (less than 500 m in nine minutes). This gets loop-back journeys
from 60% down to 16%.

**Stop sequences are not gospel.** They are measured as 6-9x inflated on a
coherent line and 200-480x on a number reused across towns, before
`operator` joined the grain (which fixes cause 1 for post-2026-09-12 data).
The contract documents this; the API consumer is warned not to trust them
as a next-stop oracle.

**Stationary time is trimmed.** The first and last pings of a journey are
often the bus sitting still at the stand. Trimming them moved the
regression's r² from 0.52 to 0.69.

## 🔒 Security Posture

* **No public ingress:** `api`, `osrm`, `archiver` and `ingestor` are reachable only from inside `ghost-net`; only `tiles` and `db` expose host ports, both configurable in `.env`.
* **Secrets stay in `.env`:** the BODS API key and QuestDB credentials are never committed; `.env.example` documents the shape.
* **Contract-gated publish:** `build.sh` refuses to publish new marts if `contracts/validate.py` finds a violation — a broken contract leaves the previous good marts serving.

## License

MIT. See LICENSE.

---

<div align="center">
<sub><i>"Sovereignty is found in the Source."</i></sub>
</div>
