# ghost_bus — a dbt project over live UK bus positions

This project turns a firehose of bus GPS pings into five small tables that a
Flask API can answer questions from: where each line goes, what its stops are in
order, how punctual it is, and — the interesting one — how long a bus with
*X* kilometres left to run will actually take.

It is deliberately a *small* warehouse. Everything lives in one DuckDB file,
`ghost_bus.duckdb`, which Flask opens read-only. There is no cluster, no
scheduler and no model artefact: the fitted regression coefficients *are* a
table.

---

## Quick start

Every command must be run **from this directory** (`dbt/`). The sources point at
relative paths (`data/bus_positions_*.parquet`), so running from the repo root
will fail to find them.

```bash
cd dbt
./build.sh                    # extract, build, test, publish. This is the one to use.
SKIP_EXTRACT=1 ./build.sh     # rebuild from the parquet already in data/
KEEP_BUILD=1   ./build.sh     # keep the scratch warehouse for poking at
```

Or the steps by hand:

```bash
cd dbt

# 1. pull the last 21 days out of QuestDB into data/*.parquet (~750 MB, several min)
QUESTDB_HTTP=http://localhost:9001 EXTRACT_DAYS=21 .venv/bin/python extract.py

# 2. build every model (into the SCRATCH file -- see "Two files", below)
.venv/bin/dbt run --profiles-dir .

# 3. assert the results are sane
.venv/bin/dbt test --profiles-dir .

# 4. browse the docs, including the DAG
.venv/bin/dbt docs generate --profiles-dir .
.venv/bin/dbt docs serve --profiles-dir .
```

### Two files, and why

`dbt run` builds into **`ghost_bus_build.duckdb`**, a scratch warehouse. Only
once `dbt test` has passed does `build.sh` copy the five marts into a fresh
file and `mv` it over **`ghost_bus.duckdb`**, which is the one Flask opens.

This is not tidiness, it is two bugs' worth of scar tissue:

- DuckDB takes an **exclusive lock** on its database for the whole build. When
  dbt built in place, every read-only open from the API failed with
  `Conflicting lock is held`, and all ~4,200 buses on the live map went grey
  for the several minutes the rebuild took. A rename within one filesystem is
  atomic, so readers now see either the whole old file or the whole new one.
- Publishing only **after the tests pass** means a bad build leaves the last
  good marts serving, instead of replacing them with something known to be
  wrong.

Copying only the marts is also what keeps the served file small: the
materialised intermediates are ~95% of the scratch warehouse (2.9 GB against
about 50 MB of marts), and the API reads none of them. DuckDB does not shrink a
file on `DROP`, so copying into a fresh database is what actually reclaims the
space.

`build.sh` also runs everything under `nice`/`ionice`. DuckDB will take every
core it can get; at 4 threads it drove the load average past 10 on a 6-core box
that also runs QuestDB, the ingestor and the API, and the API's p50 response
went from 1.3 s to 15.6 s -- far enough for nginx to start returning 504s. The
profile caps dbt at 3 threads and 6 GB for the same reason. A slower rebuild is
fine; the website falling over while it runs is not.

Useful during development:

```bash
.venv/bin/dbt run  --profiles-dir . --select stg_bus_pings          # one model
.venv/bin/dbt run  --profiles-dir . --select stg_bus_pings+         # it and everything downstream
.venv/bin/dbt run  --profiles-dir . --select +mart_eta_model        # it and everything it needs
.venv/bin/dbt test --profiles-dir . --select mart_eta_model
```

There is no `packages.yml` and no `dbt deps` step: this project needs no
packages. It would have taken a dependency on `dbt_utils` for exactly one macro
(`unique_combination_of_columns`), and writing those four assertions as singular
tests in `tests/` was cheaper than carrying the dependency.

---

## Why the models are laid out in three layers

dbt projects conventionally split into **staging → intermediate → marts**, and
the split earns its keep here rather than being ceremony. Each layer has one job
and is allowed to do nothing else.

### `models/staging/` — one model per source, views

Rename, cast, and per-row derivation. No joins, no aggregation, no business
logic. `stg_bus_pings` drops physically impossible rows and derives timestamps
and bands; `stg_naptan_stops` decides once and for all what counts as a bus
stop.

Views, because they cost nothing and always reflect whatever parquet files are
currently in `data/`.

The payoff is debuggability. When a number in a mart looks wrong, there is
exactly *one* place where a unit or a type could have gone astray — and it is a
model you can read top to bottom in a minute.

### `models/intermediate/` — the actual work

Four models, and this is where all the difficulty lives:

| model | what it does |
|---|---|
| `int_bus_journeys` | cuts each vehicle's endless ping stream into individual journeys |
| `int_journey_shape` | one row per journey; drops implausible ones; decides direction |
| `int_ping_remaining` | the training set: for each ping, how far and how long is left |
| `int_ping_stop_match` | snaps every ping to the nearest bus stop within 120 m |

`int_journey_shape` is also where **direction** is decided, by one of two rules.
Linear routes are split on the journey's first-to-last compass bearing against
the line's modal bearing. Circular routes cannot be — a loop ends where it
started, so its bearing is noise and identical loops would be scattered at
random between the two directions — so those are split on the **sign of the
enclosed area** (the shoelace formula), which asks "which way round" instead of
"which way along" and always agrees between two buses running the same loop the
same way. A line counts as circular when the median of `straight_km / path_km`
across its journeys is below 0.35.

Intermediate models are not for the API to read. They exist so that no single
model has to be understood all at once, and so that expensive work is done once
and reused. `int_bus_journeys` and `int_ping_stop_match` are materialised as
**tables** rather than views for exactly that reason — three models scan the
first one, and recomputing seven window functions over 20 million rows three
times would dominate the whole build.

### `models/marts/` — what Flask reads, tables

Five tables whose names and columns are frozen in `CONTRACT.md`. They are plain,
wide and boring on purpose: all the judgement happened upstream, and a mart is
just the shape the application wants. They are real tables, not views, because a
long-lived Flask process reads them over a read-only handle.

---

## The hard parts of the data, and what was done about them

Worth reading before trusting any number out of this project.

**A bus_id is not a bus.** Operators mint their own vehicle references, so
`3104` is fifteen different physical buses in fifteen different towns, and their
pings interleave in time. `operator` would disambiguate them but is NULL in all
historical data. `int_bus_journeys` therefore breaks a journey whenever
consecutive pings imply more than 120 km/h — a bus cannot do that, but a
reference collision can.

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

**There is no direction in the data.** The feed's `DirectionRef` only began
arriving on 2026-09-12, so it is NULL for the whole training set — and a stop
sequence without a direction is useless, because the outbound and inbound orders
interleave into one meaningless list. `int_journey_shape` therefore *infers* a
direction from each journey's first-to-last compass bearing, split against the
line's modal bearing. It is a heuristic, it is labelled as one throughout, and
it will bucket circular routes badly. It also retires itself: the feed's real
direction always wins where it exists, so the heuristic fades out as post-patch
data accumulates.

**The stop sequence is not a next-stop oracle, and I measured how far off it
is.** Walk the stops of one `(line, direction)` in `seq` order, sum the
distance, divide by the route's real length — a correct sequence scores ~1.
`mart_line_stops` scores **6–9x** on a geographically coherent line and
**200–480x** on a line whose number is reused across towns. The dominant cause
is that journeys on one line start in many different places (the 192 outbound
has 24 distinct start points: short workings, part-route runs, depot
departures), so "seconds from origin" pools populations that are not
comparable. Restricting to full-length runs gets 9.0x → 6.1x; ordering by
distance instead of time gets 9.0x → 8.6x. Neither is a fix, so neither is
implemented — machinery that improves the number without making it usable only
buys false confidence. `seq` is a rough "how far along the route is this stop",
nothing more.

**A line number is not a route.** Operators choose service numbers
independently, so `88` is a Sheffield cross-city route *and* a Wilmslow–Hale
route *and* something in rural Lancashire. `mart_line_stops` and
`mart_line_localities` merge them, because the contract's keys have no operator
column. Distinctive numbers (the 192, the 82) read as clean geographic
progressions; low, widely-reused numbers do not. The fix is to add `operator` to
those mart grains once post-2026-09-12 data makes it available — which is a
contract change, not a modelling one.

**The clock is not UTC.** Every ping is *stored* in UTC, but every temporal
*bucket* has to be local. The UK runs UTC+1 from late March to late October, so
bucketing on UTC puts the morning peak an hour early and files a 00:30 Saturday
ping under Friday — a bug that leaves no trace, because every value is still a
legal band name and the model is simply fitted against the wrong hour.
`stg_bus_pings` derives `ping_local_ts` with `AT TIME ZONE 'Europe/London'`, and
`hour_band` and `day_type` come from that and never from `ping_ts`.
`tests/assert_temporal_buckets_are_local.sql` fails if anything reverts.

**The date range has holes, and partial days lie.** The feed was out from
2026-08-28 to 08-30, so those days have no parquet file at all. Nothing may
assume consecutive dates. The subtler problem is the *edges*: a journey that was
still running when the data stops does not end, it is **cut**, and its last
observed ping is not its destination. Left in, those journeys actively teach the
regression that a bus with half a route still ahead of it arrived in zero
seconds — and they are the fastest-looking journeys in the set, so they bias the
slope low. `int_journey_shape` finds the coverage edges from the data itself
(10-minute buckets with no successor within 20 minutes) and discards journeys
that end at one. That handles the outage, the end of today's partial file, and
whatever the feed does next, without hardcoding a single date.

**Bank holidays are not weekdays.** `day_type` is pure calendar arithmetic by
design, so the late-August bank holiday would be filed as `weekday` while
actually running a Sunday-ish service. The one such day in the current window
(2026-08-31) is excluded outright in `stg_bus_pings` rather than mislabelled.
That is a deliberately small hammer: it must not grow into a holiday calendar.
If the window ever spans many holidays, the right answer is a real `holiday`
day_type fed by a proper calendar source.

**Distances are chords, not roads.** Pings are 3-minute samples, so summing the
straight hops between them cuts every corner and understates real road distance
by perhaps 10–20% on winding routes. It is *consistent*, which is what a
regression needs, but it means the fitted slope is seconds per *observed* km and
not a claim about the odometer.

---

## The grid trick (`int_ping_stop_match`)

Worth its own section, because it is the one piece of genuine algorithm here.

We need the nearest bus stop to each ping, within 120 m. There are ~13 million
pings and ~344 thousand stops. The obvious query —

```sql
select ... from pings p cross join stops s where haversine(p, s) < 0.12
```

— is 4.5 × 10¹² distance calculations. That is not slow; that is impossible.

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

Result: 6.1 million matches in about 20 seconds, at a median of 30 m.

---

## How the ETA regression works

The model is deliberately the simplest thing that could work, and it is worth
understanding why that is a feature.

### The idea

For every ping of every journey we know two numbers: how many kilometres of
route the bus still has in front of it (`remaining_km`) and how many seconds it
actually took to cover them (`remaining_secs`). That is `int_ping_remaining`,
and it is ~6 million training rows.

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
    line, direction, hour_band,
    regr_slope(remaining_secs, remaining_km)     as slope_s_per_km,
    regr_intercept(remaining_secs, remaining_km) as intercept_s,
    regr_r2(remaining_secs, remaining_km)        as r2,
    regr_count(remaining_secs, remaining_km)     as n_obs
from int_ping_remaining
group by line, direction, hour_band
```

That is the entire model. No Python, no scikit-learn, no serialised artefact, no
training/serving skew — the coefficients *are* the mart, and inference is one
multiply and one add inside Flask.

### Why it is fitted per (line, direction, day_type, hour_band)

Because pace is not a property of buses in general. The 192 outbound averages
14.7 km/h in the morning peak and 19.2 km/h at night; pooling those would
predict neither. Splitting by line captures the route, by direction captures
asymmetric traffic, by `hour_band` captures congestion, and by `day_type`
captures the fact that a Sunday service is a different service — fewer
journeys, fewer stops actually served, and much lighter traffic.

Splitting three ways thins every cell, which is a real cost and is why the
fallback chain below has six rungs rather than four.

The bands are frozen in `macros/hour_band.sql` and must match
`backend/app.py` exactly. If they drift, every lookup silently misses its band
and quietly degrades to a coarser model — a failure with no error message,
which is the worst kind.

### Why there are fallback rows

A lot of (line, direction, band) triples have too few observations to fit
anything trustworthy, and the API must never fail to find a model. So the same
table is emitted at four widths, each strictly coarser:

```
(line, direction, day_type, hour_band)   the real thing
(line, direction, day_type, '*')         that day pattern, all day
(line, direction, '*',      hour_band)   that time of day, any day
(line, direction, '*',      '*')         the route, both pooled
(line, '*',       '*',      '*')         the service number
('*',  '*',       '*',      '*')         the network average, always present
```

Flask tries them in that order and is guaranteed a hit. Rungs 2 and 3 are
siblings rather than nested, and the order between them is a judgement: given a
thin cell we would rather keep the day pattern and lose the hour than the
reverse, because a Sunday fitted from weekday data is worse than a Sunday
fitted across the whole day.

The six fits are generated by a Jinja loop in `mart_eta_model.sql` rather than
written out six times — each entry lists which columns are real at that grain
and everything else becomes the literal `'*'`. **Every fallback is
refitted from the underlying pings at that width** — not averaged from the finer
rows. Averaging slopes would weight a 300-observation grain the same as a
300,000-observation one, which is exactly backwards.

A grain is published only if it has ≥ 200 observations, a positive slope, and an
implied speed between 3 and 90 km/h. A grain that fails is simply omitted, so
the lookup falls through to the next coarser row. No model is better than a
wrong model, and `tests/assert_eta_model_speeds_are_plausible.sql` asserts the
guard is still wired up.

### Reading the output

Typical fitted values on this data:

| line | direction | band | slope s/km | intercept s | r² | n |
|---|---|---|---|---|---|---|
| 192 | outbound | am_peak | 245 | 486 | 0.94 | 8,927 |
| 192 | outbound | inter | 266 | 418 | 0.95 | 13,232 |
| 192 | outbound | night | 188 | 412 | 0.96 | 2,257 |
| `*` | `*` | `*` | 171 | 749 | 0.69 | 5,965,085 |

Slopes of 170–270 s/km are 13–21 km/h. That looks slow until you remember that
this is a stopping urban bus: TfL's whole-network average is about 14 km/h. Peak
bands are consistently slower than night bands, which is the sanity check that
matters most — if that pattern were absent or reversed, the journey segmentation
would be wrong.

The intercepts are large (300–750 s) and that is real rather than a bug. Two
things feed it: a bus spends disproportionate time on the final kilometre into a
town centre, and within one grain a short slow journey is pooled with a long
fast one. Finer grains have smaller intercepts, which is the expected direction —
the global row's 749 s is the worst of them precisely because it pools
everything.

### What would improve it

In rough order of value:

1. **Use the real `DirectionRef`.** It is arriving now. Inference goes away by
   itself as post-patch days accumulate.
2. **Use `journey_ref` for segmentation.** Also arriving now; it makes every
   heuristic in `int_bus_journeys` redundant.
3. **Fit per stop rather than per line.** "Seconds from here to the end" varies
   along a route in ways a single slope cannot express.
4. **Add the day of week.** A Saturday inter-peak is not a Tuesday inter-peak.

---

## Layout

```
dbt/
├── build.sh                      one-shot extract + run + test + row counts
├── dbt_project.yml               project config, per-layer materialisations
├── profiles.yml                  DuckDB target (kept in-project, hence --profiles-dir .)
├── extract.py                    QuestDB -> data/bus_positions_YYYY-MM-DD.parquet
├── data/                         parquet days + naptan_stops.csv (gitignored)
├── macros/
│   ├── haversine.sql             great-circle km between two lat/lon pairs
│   ├── bearing.sql               compass bearing, and angle between two bearings
│   ├── hour_band.sql             LOCAL hour -> traffic regime (FROZEN vs backend/)
│   ├── day_type.sql              LOCAL date -> weekday/sat/sun (FROZEN vs backend/)
│   ├── locality_norm.sql         the search-key normalisation (FROZEN vs backend/)
│   └── ping_columns.sql          compile-time probe for the schedule columns
├── models/
│   ├── staging/                  _sources.yml, stg_bus_pings, stg_naptan_stops
│   ├── intermediate/             journeys, shape, training set, stop match
│   └── marts/                    the five contract tables
└── tests/                        singular assertions (composite keys, invariants)
```

### A note on `macros/ping_columns.sql`

The daily parquet files genuinely have *different schemas*: the ingestor gained
nine schedule columns on 2026-09-12, so files written before that are narrower.
`read_parquet(..., union_by_name=true)` handles the case where *some* files have
a column — it fills the rest with NULL. But if *no* file has it, the column does
not exist and every model referencing it fails to compile.

So `ping_columns()` asks DuckDB once, at parse time, what the union actually
contains, and `stg_bus_pings` emits `cast(null as varchar) as journey_ref` for
whatever is missing. It is the one piece of Jinja in the project that does
something a plain SQL file could not, and it is what lets the whole thing build
on a box that has only historical data.
