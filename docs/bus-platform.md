# Bus Platform

The pipeline from live vehicle GPS pings to a working ETA prediction, in
four stages.

```
BODS SIRI-VM feed (polled every 60s, GB mainland bbox)
  ↓  ingestor/ingest.py
QuestDB  —  bus_positions (append-only, partitioned by day)
  ↓  dbt/build.sh, nightly at 03:00
DuckDB marts  —  ghost_bus.duckdb (staging → intermediate → marts)
  ↓  backend/app.py, opened read-only, reloaded on mtime change
Flask API  →  MapLibre map on slaycoach.quest/bus-map/
```

## Ingest: `ingestor/ingest.py`

Polls the DfT Bus Open Data Service (BODS) SIRI-VM AVL feed every 60
seconds, bounded to a bounding box covering mainland Great Britain
(`-6.3,49.9,1.8,58.75`). The bbox is rectangular, so it can't carve out
Northern Ireland, the Isle of Man or the Channel Islands as enclaves — but
it does exclude Orkney, Shetland, the Outer Hebrides and the Scillies, at a
measured cost of 0.5% of the feed (129 of 24,536 vehicles GB-wide, all
excluded islands combined). Northern Ireland, Man and the Channel Islands
aren't in BODS regardless, so nothing is lost there.

Each `VehicleActivity` in the SIRI XML becomes one Influx Line Protocol
line written to QuestDB's `/write` endpoint. Vehicle reference and line
number become low-cardinality ILP tags (SYMBOL columns); everything else —
position, bearing, journey/origin/destination refs, aimed times — becomes a
field. The ingestor writes best-effort: a `VehicleActivity` that fails to
parse is skipped, not fatal to the poll.

The feed itself is considerably less trustworthy than it looks — see
[Contracts](contracts.md) for what it does and does not guarantee.

## Storage: QuestDB, 22 days hot

`bus_positions` is an append-only QuestDB table, partitioned by day. Hot
retention is 22 days (`RETENTION_DAYS` in `.env`); anything older is moved,
not deleted — see [Operations](operations.md#archiving-not-deleting) for
how `archiver/archive.py` does that.

## Transform: dbt, staging → intermediate → marts

`dbt/` is a DuckDB-backed dbt project laid out in the conventional three
layers, each doing exactly one job:

- **`models/staging/`** — one model per source, organised by source system,
  views. Rename, cast, and per-row derivation only; no joins, no business
  logic. `staging/bods/stg_bods__vehicle_pings` drops physically impossible
  rows and derives local-time bands; `staging/naptan/stg_naptan__stops`
  decides once what counts as a bus stop.
- **`models/intermediate/`** — the actual work, four models:
    - `int_pings_segmented_to_journeys` — cuts each vehicle's endless ping
      stream into individual journeys, breaking on 120 km/h speed jumps (a
      bus_id collision, since a real bus can't do that) and on layovers
      (under 500 m of movement in 9 minutes, since parked buses keep
      transmitting).
    - `int_journeys_shaped_and_directed` — one row per journey; drops
      journeys cut off by a data-coverage edge; infers direction (see
      below).
    - `int_pings_measured_to_journey_end` — the ETA model's training set:
      for every ping, how far and how long is left in its journey.
    - `int_pings_matched_to_stops` — snaps every ping to the nearest stop
      within 120 m, using a grid-binning join (see below) instead of a full
      cross join.

  `int_pings_segmented_to_journeys` and `int_pings_matched_to_stops` are
  materialized as **incremental tables**, not views: three downstream
  models scan the journeys table, and recomputing window functions over
  the full retention window every run would dominate the build. Each run
  reprocesses only the trailing ~2 days; `FULL_REFRESH=1` forces a
  from-scratch rebuild of both (see [Operations](operations.md)).
- **`models/marts/`** — five tables, frozen by `contracts/bus-marts.odcs.yaml`,
  read by the Flask API: `eta_models`, `line_stops`, `line_localities`,
  `line_punctuality`, `localities`. Named as plain entities rather than
  `mart_*`-prefixed, per dbt's own structure guide. Real tables, not
  views — a long-lived Flask process reads them over a read-only handle.

### Direction is inferred, not given

The feed's `DirectionRef` only started arriving on 2026-09-12, so it's
`NULL` for all older data. `int_journeys_shaped_and_directed` infers direction two ways:
linear routes are split on the journey's first-to-last compass bearing
against the line's modal bearing; circular routes (median
`straight_km / path_km` below 0.35 — a loop's endpoints are close together)
are split on the sign of the enclosed area (shoelace formula), since a
loop's bearing carries no information about which way round it ran. This
heuristic retires itself automatically as real `DirectionRef` data
accumulates.

### The grid trick for stop matching

Matching ~13M pings against ~344k stops within 120 m is a 4.5×10¹² distance
calculation as a naive cross join — not slow, impossible. Instead, both
pings and stops are binned onto a fixed grid (`floor(lat*500), floor(lon*500)`,
~0.002° cells), each stop is replicated into its own cell plus its eight
neighbours, and pings join to that on the integer cell pair — a hash join,
linear in size. Haversine distance is only computed on the handful of
candidates that survive. Cell size and match radius are chosen together: a
0.002° cell is ~222m N–S and ~131m E–W at UK latitudes, so a ping at the
very edge of its cell still has a full neighbouring cell of guaranteed
coverage. Result: ~6.1M matches in about 20 seconds, median match distance
30m.

## The ETA model

One ordinary-least-squares regression per `(operator, line, direction,
day_type, hour_band)` grain, fitted directly in SQL with DuckDB's
`regr_slope` / `regr_intercept` / `regr_r2` aggregates — no Python, no
serialized model artifact, no training/serving skew:

```
remaining_secs = intercept_s + slope_s_per_km × remaining_km
```

`slope_s_per_km` is the service's pace; `3600 / slope` gives its mean speed
in km/h. Fitted per-grain because pace genuinely varies by all four axes —
e.g. the 192 outbound averages 14.7 km/h in the morning peak against
19.2 km/h at night, and pooling those would predict neither well.

Grains with too few observations fall back through eleven progressively
coarser rungs, ending at a global network-average row that is always
present. The operator-specific rungs only exist where operator is not
null, so every pre-2026-09-12 ping can only ever feed the `'*'`-operator
half of the chain — the original six-rung version this table used to be
in full:

```
(operator, line, direction, day_type, hour_band)   this operator, the real thing
(operator, line, direction, day_type, '*')
(operator, line, direction, '*',      hour_band)
(operator, line, direction, '*',      '*')
(operator, line, '*',       '*',      '*')
('*',      line, direction, day_type, hour_band)
('*',      line, direction, day_type, '*')
('*',      line, direction, '*',      hour_band)
('*',      line, direction, '*',      '*')
('*',      line, '*',       '*',      '*')
('*',      '*',  '*',       '*',      '*')         always present
```

Every fallback is refitted directly from the underlying pings at that
width, never averaged from finer rows — averaging slopes would weight a
300-observation grain the same as a 300,000-observation one. A grain
publishes only with ≥200 observations, a positive slope, and an implied
speed between 3–90 km/h; a grain that fails the guard is simply omitted so
the lookup falls through.

## Serving: `backend/app.py`

A Flask API with five routes:

| Route | Purpose |
|---|---|
| `GET /buses` | Live vehicle positions as GeoJSON, filtered to positions under 10 minutes old |
| `GET /bus/<bus_id>` | Detail for one vehicle: position, model grain used, predicted arrival |
| `GET /route` | Stop sequence, geometry and live buses for one line |
| `GET /search` | Locality search against `localities` |
| `GET /model-info` | Marts freshness, model coverage, and any degraded-state note |

The marts are optional by design: if `ghost_bus.duckdb` is missing,
mid-rewrite, or a table inside it is absent, the API still answers — with
`"unknown"` status and null predictions — rather than 500ing. See
[Operations](operations.md#the-api-never-needs-a-restart) for how it
notices a rebuild without being restarted.

The feed's own unreliability leaks all the way to this layer: a vehicle
whose most recent position (`recorded_at`) is stale is dropped from
`/buses` even though the record itself just arrived, and a schedule
(`dest_aimed_arr`) more than 3 hours in the past or 12 hours in the future
is treated as untrusted rather than used to compute a delay.
