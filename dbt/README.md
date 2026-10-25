# wraith_bus_ingestor — a dbt project over live UK bus positions

This project turns a firehose of bus GPS pings into three small tables that a
Flask API can answer questions from: which places have bus stops, how punctual
each line is, and — the interesting one — how long a bus with *X* kilometres left
to run will actually take. (Routes and stop sequences used to be two more tables;
they now come from the published timetable, see ADR-0008.)

**New to dbt?** Start with [Learn dbt](../docs/learn-dbt/index.md), a short
hands-on course that uses this very project, and never run `dbt` in this folder
(it is the nightly build's): use `ops/dbt_sandbox.sh`.

It is deliberately a *small* warehouse. Everything lives in one DuckDB file,
`wraith_bus_ingestor.duckdb`, which Flask opens read-only. There is no cluster, no
scheduler and no model artefact: the fitted regression coefficients *are* a
table.

---

## Quick start

Every command must be run **from this directory** (`dbt/`). The sources point at
relative paths (`data/bus_positions_*.parquet`), so running from the repo root
will fail to find them.

There is no separate `dbt/.venv` -- dbt runs under the repo root's own
`.venv` (`uv sync --all-extras` there installs `dbt-core` + `dbt-duckdb`,
their own optional group, alongside Flask and everything else), referenced
below as `../.venv`. See `docs/adr/0005-dbt-v2-duckdb-alpha-migration.md`
for why this isn't dbt v2 + DuckDB 2.0 -- tried, rolled back the same day.

```bash
cd dbt
./build.sh                    # extract, build, test, validate contracts, publish. This is the one to use.
SKIP_EXTRACT=1 ./build.sh     # rebuild from the parquet already in data/
FULL_REFRESH=1 ./build.sh     # dbt run --full-refresh: rebuild segmentation from data/, keep the two history tables
```

Or the steps by hand:

```bash
cd dbt

# 1. pull the last 8 days out of QuestDB into data/*.parquet (mainland GB, several min)
# Match dbt_project.yml's `extract_days` var (see macros/retention_days.sql)
# and .env's RETENTION_DAYS -- all three should agree on the same window.
# A day that already has a data/bus_positions_<day>.done marker is skipped;
# delete the marker to pull that day again.
QUESTDB_HTTP=http://localhost:9001 EXTRACT_DAYS=8 ../.venv/bin/python extract.py

# 2. build every model (into the SCRATCH file -- see "Two files", below)
../.venv/bin/dbt run --profiles-dir .

# 3. assert the results are sane
../.venv/bin/dbt test --profiles-dir .

# 4. browse the docs, including the DAG
../.venv/bin/dbt docs generate --profiles-dir .
../.venv/bin/dbt docs serve --profiles-dir .
```

### Two files, and why

`dbt run` builds into **`wraith_bus_ingestor_build.duckdb`**, a scratch warehouse. Only
once `dbt test` and the contract gate (`contracts/validate.py`, run by
`build.sh` against the candidate marts) have both passed does `build.sh` copy
the five marts into a fresh file and `mv` it over **`wraith_bus_ingestor.duckdb`**,
which is the one Flask opens.

This is not tidiness, it is scar tissue from real incidents:

- DuckDB takes an **exclusive lock** on its database for the whole build. When
  dbt built in place, every read-only open from the API failed with
  `Conflicting lock is held`, and every bus on the live map went grey for the
  several minutes the rebuild took. A rename within one filesystem is atomic,
  so readers now see either the whole old file or the whole new one.
- Publishing only **after the tests and the contract gate pass** means a bad
  build leaves the last good marts serving, instead of replacing them with
  something known to be wrong.

Copying only the marts is also what keeps the served file small: the
materialised intermediates are ~95% of the scratch warehouse, and the API
reads none of them. DuckDB does not shrink a file on `DROP`, so copying into a
fresh database is what actually reclaims the space.

**The scratch warehouse now persists between runs.** Three of the five
intermediate models are incremental. `int_pings_segmented_to_journeys`
reprocesses only the last couple of days each run instead of the full
retention window -- and dbt's
`is_incremental()` only takes that fast path when the target table already
exists. `int_eta_daily_stats` and `int_journey_delays` keep a daily summary
for `history_days` (8) however long the raw pings are kept, so this file is
the only place that history lives (ADR-0010). `FULL_REFRESH=1 ./build.sh`
passes `--full-refresh`, the escape hatch for a schema change or suspected
drift: it rebuilds segmentation from `data/` but no longer deletes the file,
and the two history models set `full_refresh=var('replay', false)`, so it
leaves them alone. Only a replay (`docs/operations.md`) rebuilds them, from
whatever is in `data/`.

`build.sh` also runs everything under `nice`/`ionice`. DuckDB will take every
core it can get; at 4 threads it drove the load average past 10 on a 6-core box
that also runs QuestDB, the ingestor and the API, and the API's p50 response
went from 1.3 s to 15.6 s -- far enough for nginx to start returning 504s. The
profile caps dbt at 3 threads and 6 GB for the same reason. A slower rebuild is
fine; the website falling over while it runs is not.

Useful during development:

```bash
../.venv/bin/dbt run  --profiles-dir . --select stg_bods__vehicle_pings          # one model
../.venv/bin/dbt run  --profiles-dir . --select stg_bods__vehicle_pings+         # it and everything downstream
../.venv/bin/dbt run  --profiles-dir . --select +eta_models                     # it and everything it needs
../.venv/bin/dbt test --profiles-dir . --select eta_models
```

There is no `packages.yml` and no `dbt deps` step: this project needs no
packages. It would have taken a dependency on `dbt_utils` for exactly one macro
(`unique_combination_of_columns`), and writing those assertions as singular
tests in `tests/` was cheaper than carrying the dependency.

---

## Why the models are laid out in three layers

dbt projects conventionally split into **staging → intermediate → marts**, and
the split earns its keep here rather than being ceremony. Each layer has one job
and is allowed to do nothing else. Layout and naming follow
[dbt Labs' own structure guide](https://docs.getdbt.com/best-practices/how-we-structure/1-guide-overview).

### `models/staging/` — one model per source, organised by source system, views

Rename, cast, and per-row derivation. No joins, no aggregation, no business
logic. Two source systems, two subdirectories:

- `staging/bods/` — `stg_bods__vehicle_pings` drops physically impossible rows
  and derives timestamps and bands. Named for the Bus Open Data Service, the
  actual source system; QuestDB is just the landing store the ingestor writes
  to on the way.
- `staging/naptan/` — `stg_naptan__stops` decides once and for all what counts
  as a bus stop.

Views, because they cost nothing and always reflect whatever parquet files are
currently in `data/`.

The payoff is debuggability. When a number in a mart looks wrong, there is
exactly *one* place where a unit or a type could have gone astray — and it is a
model you can read top to bottom in a minute.

### `models/intermediate/` — the actual work

Five models, and this is where all the difficulty lives:

| model | what it does |
|---|---|
| `int_pings_segmented_to_journeys` | cuts each vehicle's endless ping stream into individual journeys |
| `int_journeys_shaped_and_directed` | one row per journey; drops implausible ones; decides direction |
| `int_pings_measured_to_journey_end` | the training set: for each ping, how far and how long is left |
| `int_eta_daily_stats` | the training set reduced to per-day sufficient statistics, kept for `history_days` |
| `int_journey_delays` | one row per journey `line_punctuality` can use, kept for `history_days` |

`int_journeys_shaped_and_directed` is also where **direction** is decided, by
one of two rules. Linear routes are split on the journey's first-to-last
compass bearing against the line's modal bearing. Circular routes cannot be —
a loop ends where it started, so its bearing is noise and identical loops
would be scattered at random between the two directions — so those are split
on the **sign of the enclosed area** (the shoelace formula), which asks "which
way round" instead of "which way along" and always agrees between two buses
running the same loop the same way. A line counts as circular when the median
of `straight_km / path_km` across its journeys is below 0.35.

Intermediate models are not for the API to read. They exist so that no single
model has to be understood all at once, and so that expensive work is done
once and reused. `int_pings_segmented_to_journeys` is the most expensive step
in the project (recomputing window functions over the full retention window
would otherwise dominate every rebuild), so it is **incremental** rather than
a full `table` rebuild every run -- see its model header for the read/write
window and why the journey key is derived the way it is. (A second incremental
model, `int_pings_matched_to_stops`, and the stop-sequence cluster built on it
were deleted in ADR-0008: routes now come from the published GTFS timetable.)
`int_journeys_shaped_and_directed` stays a full `table` rebuild deliberately:
its per-line reference bearing is a stable aggregate across the *whole*
retention window and must not be limited to a lookback slice.

### `models/marts/` — what Flask reads, tables

Three tables whose names and columns are frozen in `CONTRACT.md` and
`contracts/bus-marts.odcs.yaml`. They are plain, wide and boring on purpose:
all the judgement happened upstream, and a mart is just the shape the
application wants. They are real tables, not views, because a long-lived
Flask process reads them over a read-only handle. Following the structure
guide, they are named as plain entities (`eta_models`, `localities`,
`line_punctuality`) rather than prefixed `mart_*`. (`line_stops` and
`line_localities` were removed in `bus-marts` 3.0.0; their job moved to the
GTFS timetable, see `contracts/gtfs-timetable.odcs.yaml`.)

---

## The hard parts of the data, and what was done about them

Worth reading before trusting any number out of this project.

**A bus_id is not a bus.** Operators mint their own vehicle references, so
`3104` is fifteen different physical buses in fifteen different towns, and
their pings interleave in time. `operator` disambiguates them where the feed
supplies one (every ping before 2026-09-12 has none), and
`int_pings_segmented_to_journeys` also breaks a journey whenever consecutive
pings imply more than 120 km/h — a bus cannot do that, but a reference
collision can.

**Buses keep transmitting while parked.** They sit on the terminus stand and
report every three minutes, so a "20 minutes with no pings" rule never fires on
a layover, and a bus working one route all day came out as a single journey of a
dozen round trips. Measured: 60% of surviving journeys ended within 30% of a
straight line of where they started. The fix is to detect the layover directly —
moved less than 500 m over the previous nine minutes — and break there. That
drops the loop-back share to 16% and makes the median journey a plausible
45 minutes and 9.8 km.

**Stationary time is not travel.** Even within a journey, the first and last
pings are often the bus standing still. Trimming the journey to its first and
last genuinely-moving ping moved the regression's r² from 0.52 to 0.69.

**Direction used to be inferred; it no longer is (retired 2026-09-23, see
`docs/ROADMAP.md` 1.2.0).** `int_journeys_shaped_and_directed` used to infer a
direction from each journey's first-to-last compass bearing (linear routes) or
the sign of its enclosed area (circular routes, shoelace formula) whenever the
feed's `DirectionRef` was NULL -- true of almost every row when this was
written, since the feed only started sending it 2026-09-12. Measured against
the build warehouse before retiring it: the feed now tags 68.1% of journeys,
and where both existed the heuristic disagreed with it 53.9% of the time --
chance level, because the heuristic's inbound/outbound split was picked per
line by which bearing bin had more journeys, never actually tied to the
feed's own convention. `direction` is now `feed_direction`, unmodified; NULL
on the rest, which nothing downstream requires non-null.

**Historical -- `line_stops` was removed by ADR-0008; kept because the finding
is why.** **The stop sequence is not a next-stop oracle, and I measured how far
off it was.** Walk the stops of one `(operator, line, direction)` in `seq` order, sum
the distance, divide by the route's real length — a correct sequence scores
~1. `line_stops` scores **6–9x** on a geographically coherent line and
**200–480x** on a line whose number is reused across towns (pooled, `'*'`
operator rows; the numbers predate operator joining the grain). The dominant
cause is that journeys on one line start in many different places (the 192
outbound has 24 distinct start points: short workings, part-route runs, depot
departures), so "seconds from origin" pools populations that are not
comparable. Restricting to full-length runs gets 9.0x → 6.1x; ordering by
distance instead of time gets 9.0x → 8.6x. Neither is a fix, so neither is
implemented — machinery that improves the number without making it usable only
buys false confidence. `seq` is a rough "how far along the route is this stop",
nothing more.

**A line number is not a route.** Operators choose service numbers
independently, so `88` is a Sheffield cross-city route *and* a Wilmslow–Hale
route *and* something in rural Lancashire. `operator` joined the grain of
`line_stops`, `line_localities` and `eta_models` on 2026-09-12, the day it
first became available in the feed (`line_stops` and `line_localities`, now
removed, had it too), which fixes this for post-patch data: a real-operator
row never merges unrelated routes. `eta_models` still falls back to the pooled
(`'*'`-operator) grain for every line it has no operator-tagged history for.

**The clock is not UTC.** Every ping is *stored* in UTC, but every temporal
*bucket* has to be local. The UK runs UTC+1 from late March to late October, so
bucketing on UTC puts the morning peak an hour early and files a 00:30 Saturday
ping under Friday — a bug that leaves no trace, because every value is still a
legal band name and the model is simply fitted against the wrong hour.
`stg_bods__vehicle_pings` derives `ping_local_ts` with `AT TIME ZONE
'Europe/London'`, and `hour_band` and `day_type` come from that and never from
`ping_ts`. `tests/assert_temporal_buckets_are_local.sql` fails if anything
reverts.

**The date range has holes, and partial days lie.** The feed had at least one
multi-day outage; days with no data have no parquet file at all, and nothing
here may assume consecutive dates. The subtler problem is the *edges*: a
journey that was still running when the data stops does not end, it is
**cut**, and its last observed ping is not its destination. Left in, those
journeys actively teach the regression that a bus with half a route still
ahead of it arrived in zero seconds — and they are the fastest-looking
journeys in the set, so they bias the slope low.
`int_journeys_shaped_and_directed` finds the coverage edges from the data
itself (10-minute buckets with no successor within 20 minutes) and discards
journeys that end at one. That handles an outage, the end of today's partial
file, and whatever the feed does next, without hardcoding a single date.

**Bank holidays are not weekdays.** `day_type` is pure calendar arithmetic by
design, so a bank holiday would be filed as `weekday` while actually running a
Sunday-ish service. Known holidays inside the extract window are excluded
outright in `stg_bods__vehicle_pings` rather than mislabelled. That is a
deliberately small hammer: it must not grow into a holiday calendar. If the
window ever spans many holidays, the right answer is a real `holiday`
day_type fed by a proper calendar source.

**Distances are chords, not roads.** Pings are 3-minute samples, so summing the
straight hops between them cuts every corner and understates real road distance
by perhaps 10–20% on winding routes. It is *consistent*, which is what a
regression needs, but it means the fitted slope is seconds per *observed* km and
not a claim about the odometer.

---

## The grid trick (`int_pings_matched_to_stops`, removed in ADR-0008)

*The model this describes was deleted on 2026-09-21, when routes moved to the
GTFS timetable. The technique is kept because it is reusable.* Worth its own
section, because it is the one piece of genuine algorithm here.

We need the nearest bus stop to each ping, within 120 m, across mainland Great
Britain: tens of millions of pings and ~344 thousand stops. The obvious query —

```sql
select ... from pings p cross join stops s where haversine(p, s) < 0.12
```

— is a cross join of both sides. That is not slow; that is impossible at this
scale.

The insight is that we never needed the nearest of *all* 344k stops. We needed
the nearest of the handful that could possibly be within 120 m. So we bin both
sides onto a fixed integer grid:

```sql
cell = (floor(lat * 500), floor(lon * 500))   -- 0.002 degrees
```

then replicate each **stop** into its own cell *plus the eight surrounding
cells* (344k × 9 = 3.1M rows, trivial), and join pings to that on the integer
pair. A hash join — linear — instead of a comparison join. Haversine is then
evaluated only on the few candidates per ping that survive.

The cell size and the match radius have to be chosen *together*. A 0.002° cell
is ~222 m north–south and ~131 m east–west at UK latitudes, so a ping sitting at
the very edge of its own cell still has a full neighbouring cell — 131 m — of
guaranteed coverage in every direction. Shrink the cells and stops just inside
the radius start being missed; grow them and the candidate lists get long again.

---

## How the ETA regression works

The model is deliberately the simplest thing that could work, and it is worth
understanding why that is a feature.

### The idea

For every ping of every journey we know two numbers: how many kilometres of
route the bus still has in front of it (`remaining_km`) and how many seconds it
actually took to cover them (`remaining_secs`). That is
`int_pings_measured_to_journey_end`.

Fit a straight line through them:

```
remaining_secs  =  intercept_s  +  slope_s_per_km × remaining_km
```

`slope_s_per_km` is the pace of the service in seconds per kilometre —
`3600 / slope` is its mean speed. `intercept_s` is the fixed overhead that does
not scale with distance.

### The fit

DuckDB has ordinary least squares built in as aggregate functions, so "training"
is a `GROUP BY`:

```sql
select
    operator, line, direction, day_type, hour_band,
    regr_slope(remaining_secs, remaining_km)     as slope_s_per_km,
    regr_intercept(remaining_secs, remaining_km) as intercept_s,
    regr_r2(remaining_secs, remaining_km)        as r2,
    regr_count(remaining_secs, remaining_km)     as n_obs
from int_pings_measured_to_journey_end
group by operator, line, direction, day_type, hour_band
```

That is the entire model. No Python, no scikit-learn, no serialised artefact, no
training/serving skew — the coefficients *are* the mart, and inference is one
multiply and one add inside Flask.

Since ADR-0010 the nightly does not run that `GROUP BY` over the raw pings.
`int_eta_daily_stats` runs it per day instead and keeps six numbers per cell
(`regr_count`, `regr_avgx`, `regr_avgy`, `regr_sxx`, `regr_sxy`, `regr_syy`);
`eta_models` pools them across days and cells with the standard pooled
formulas. That is the same line to rounding, so the raw pings no longer have
to be kept for as long as the fit covers. `tests/assert_eta_stats_match_raw_fit.sql`
checks both halves every run: the newest dates of `int_eta_daily_stats`,
pooled per line and network-wide, against `regr_*` over the raw pings, and
`eta_models`' own per-line and network rows against an independent pooling of
every day `int_eta_daily_stats` holds.

### Why it is fitted per (operator, line, direction, day_type, hour_band)

Because pace is not a property of buses in general. The 192 outbound averages
14.7 km/h in the morning peak and 19.2 km/h at night; pooling those would
predict neither. Splitting by line captures the route, by direction captures
asymmetric traffic, by `hour_band` captures congestion, and by `day_type`
captures the fact that a Sunday service is a different service — fewer
journeys, fewer stops actually served, and much lighter traffic. `operator`
sits above all of that, because a shared line number is not even the same
physical service.

Splitting five ways thins every cell, which is a real cost and is why the
fallback chain below has eleven rungs rather than five.

The bands are frozen in `macros/hour_band.sql` and must match
`backend/app.py` exactly. If they drift, every lookup silently misses its band
and quietly degrades to a coarser model — a failure with no error message,
which is the worst kind.

### Why there are fallback rows

A lot of (operator, line, direction, day_type, hour_band) combinations have too
few observations to fit anything trustworthy, and the API must never fail to
find a model. So the same table is emitted at eleven widths, each strictly
coarser:

```
(operator, line, direction, day_type, hour_band)   this operator, the real thing
(operator, line, direction, day_type, '*')         this operator, that day pattern
(operator, line, direction, '*',      hour_band)   this operator, that time of day
(operator, line, direction, '*',      '*')         this operator, the route
(operator, line, '*',       '*',      '*')         this operator, the service number
('*',      line, direction, day_type, hour_band)   any operator, the real thing
('*',      line, direction, day_type, '*')         any operator, that day pattern
('*',      line, direction, '*',      hour_band)   any operator, that time of day
('*',      line, direction, '*',      '*')         any operator, the route, both times pooled
('*',      line, '*',       '*',      '*')         any operator, the service number
('*',      '*',  '*',       '*',      '*')         the network average, always present
```

Flask tries them in that order and is guaranteed a hit. The operator-specific
rungs only exist where operator is not null, so every pre-2026-09-12 ping can
only ever feed the `'*'`-operator rungs — the original six-rung chain this
table used to be in full. Rungs 2/3 (and 7/8) are siblings rather than nested,
and the order between them is a judgement: given a thin cell we would rather
keep the day pattern and lose the hour than the reverse, because a Sunday
fitted from weekday data is worse than a Sunday fitted across the whole day.

The eleven fits are generated by a Jinja loop in `eta_models.sql` rather than
written out eleven times — each entry lists which columns are real at that
grain and everything else becomes the literal `'*'`. **Every fallback is
refitted at that width**, from the pooled daily statistics, which is the same
fit the underlying pings would give — not averaged from the finer rows.
Averaging slopes would weight a 300-observation grain the same as a
300,000-observation one, which is exactly backwards.

A grain is published only if it has ≥ 200 observations, a positive slope, and an
implied speed between 3 and 90 km/h. A grain that fails is simply omitted, so
the lookup falls through to the next coarser row. No model is better than a
wrong model, and `tests/assert_eta_model_speeds_are_plausible.sql` asserts the
guard is still wired up.

### What would improve it

In rough order of value:

1. **Use `journey_ref` for segmentation.** Arriving in the feed now; it makes
   every heuristic in `int_pings_segmented_to_journeys` redundant.
2. **Fit per stop rather than per line.** "Seconds from here to the end" varies
   along a route in ways a single slope cannot express.
3. **Add the day of week.** A Saturday inter-peak is not a Tuesday inter-peak.

---

## Layout

```
dbt/
├── build.sh                      one-shot extract + run + test + validate + publish
├── dbt_project.yml               project config, per-layer materialisations
├── profiles.yml                  DuckDB target (kept in-project, hence --profiles-dir .)
├── extract.py                    QuestDB -> data/bus_positions_YYYY-MM-DD.parquet
├── data/                         parquet days + naptan_stops.csv (gitignored)
├── macros/
│   ├── _macros.yml                documents every macro below
│   ├── haversine.sql              great-circle km between two lat/lon pairs
│   ├── speed.sql                  avg_speed_kmh: path_km over duration_secs
│   ├── operator_key.sql           no_operator_sentinel: the pre-2026-09-12 coalesce
│   ├── gb_bbox.sql                in_gb_bbox: the GB ingest bounding box filter
│   ├── hour_band.sql              LOCAL hour -> traffic regime (FROZEN vs backend/)
│   ├── day_type.sql               LOCAL date -> weekday/sat/sun (FROZEN vs backend/)
│   ├── locality_norm.sql          the search-key normalisation (FROZEN vs backend/)
│   ├── ping_columns.sql           compile-time probe for the schedule columns
│   ├── retention_days.sql         how many days of raw pings dbt keeps (extract_days)
│   └── history_days.sql           how many days the two history models keep
├── models/
│   ├── staging/
│   │   ├── bods/                  _bods__sources.yml, _bods__models.yml, stg_bods__vehicle_pings
│   │   └── naptan/                _naptan__sources.yml, _naptan__models.yml, stg_naptan__stops
│   ├── intermediate/              journeys, shape, training set, daily ETA stats, journey delays (+ _intermediate__models.yml)
│   └── marts/                     the five contract tables (+ _marts__models.yml)
└── tests/                         singular assertions (composite keys, invariants)
```

### A note on `macros/ping_columns.sql`

The daily parquet files genuinely have *different schemas*: the ingestor gained
nine schedule columns on 2026-09-12, so files written before that are narrower.
`read_parquet(..., union_by_name=true)` handles the case where *some* files have
a column — it fills the rest with NULL. But if *no* file has it, the column does
not exist and every model referencing it fails to compile.

So `ping_columns()` asks DuckDB once, at parse time, what the union actually
contains, and `stg_bods__vehicle_pings` emits `cast(null as varchar) as
journey_ref` for whatever is missing. It is the one piece of Jinja in the
project that does something a plain SQL file could not, and it is what lets
the whole thing build on a box that has only historical data.
