# Backend

<a id="app"></a>

## app

wraith-bus-ingestor backend: the live bus API.

Serves GeoJSON straight off QuestDB and enriches it with ETA regressions read
out of the dbt marts (a DuckDB file built by a separate job). The marts are
optional by design: if the file is missing, half-written or a table is absent
the API still answers, with status "unknown" and null predictions, rather
than 500ing.

Must never fail a request because the marts, the GTFS database or QuestDB's
optional columns are unavailable (degrade to null predictions instead), and
must never interpolate unvalidated user input into SQL: bus_id, operator and
line are whitelisted by regex first.

<a id="app.QUESTDB_TIMEOUT"></a>

#### QUESTDB\_TIMEOUT

seconds; keeps any request under the ~10s budget

<a id="app.MARTS_TTL"></a>

#### MARTS\_TTL

only stat the marts file once a minute

<a id="app.LATE_THRESHOLD_S"></a>

#### LATE\_THRESHOLD\_S

delay_s > 300  -> 'late'

<a id="app.EARLY_THRESHOLD_S"></a>

#### EARLY\_THRESHOLD\_S

delay_s < -60  -> 'early'

<a id="app.MAX_SCHEDULE_AGE_S"></a>

#### MAX\_SCHEDULE\_AGE\_S

aimed arrival this far in the past -> untrusted

<a id="app.MAX_SCHEDULE_AHEAD_S"></a>

#### MAX\_SCHEDULE\_AHEAD\_S

...or this far in the future

<a id="app.MIN_SCHEDULED_JOURNEY_S"></a>

#### MIN\_SCHEDULED\_JOURNEY\_S

shorter than this and the fraction is meaningless

<a id="app.MIN_ROUTE_SPAN_S"></a>

#### MIN\_ROUTE\_SPAN\_S

origin and destination must be distinguishable

<a id="app.MIN_ROUTE_KM"></a>

#### MIN\_ROUTE\_KM

...and far enough apart to measure progress along

<a id="app.MIN_TERMINUS_SEPARATION_KM"></a>

#### MIN\_TERMINUS\_SEPARATION\_KM

closer than this and the route is a loop

<a id="app.ARRIVED_KM"></a>

#### ARRIVED\_KM

this close to the destination = arrived, not late

<a id="app.ARRIVED_FRACTION"></a>

#### ARRIVED\_FRACTION

...or this far through the journey by progress

<a id="app.MIN_STOP_HOP_S"></a>

#### MIN\_STOP\_HOP\_S

adjacent stops often share a median time

<a id="app.MERGED_ROUTE_HOP_KM"></a>

#### MERGED\_ROUTE\_HOP\_KM

consecutive stops further apart than this are suspect

<a id="app.STOP_RADIUS_KM"></a>

#### STOP\_RADIUS\_KM

"near a stop" = within 100 m

<a id="app.log_once"></a>

### log\_once

```python
def log_once(key, message)
```

Complain about a broken dependency once, not once per request.

**Arguments**:

- `key` - Identifier of the failure; only the first message per key is printed.
- `message` - Text to print, prefixed with "[bus] ".
  

**Returns**:

  None.

<a id="app.haversine_km"></a>

### haversine\_km

```python
def haversine_km(lat1, lon1, lat2, lon2)
```

Great-circle distance in km.

**Arguments**:

- `lat1` - Latitude of the first point, degrees.
- `lon1` - Longitude of the first point, degrees.
- `lat2` - Latitude of the second point, degrees.
- `lon2` - Longitude of the second point, degrees.
  

**Returns**:

  Distance in kilometres.

<a id="app.classify_status"></a>

### classify\_status

```python
def classify_status(delay_s)
```

RAG bucket for a delay in seconds. See the threshold block above.

**Arguments**:

- `delay_s` - Delay in seconds (positive is late), or None.
  

**Returns**:

  One of 'early', 'on_time', 'late' or 'unknown' (when delay_s is None).

<a id="app.normalise_query"></a>

### normalise\_query

```python
def normalise_query(q)
```

Normalise a query to match localities.locality_norm.

Upper, punctuation stripped, whitespace collapsed, trimmed.

**Arguments**:

- `q` - Raw user search text.
  

**Returns**:

  The normalised string; empty when nothing remains.

<a id="app.local_zone"></a>

### local\_zone

```python
def local_zone()
```

Europe/London, or UTC with one logged warning if the zone data is absent.

zoneinfo is stdlib but python:3.11-slim carries no zone database, so the
image pip-installs `tzdata`. If that ever goes missing we must degrade, not
crash: on UTC every band is an hour out during BST and the grain quietly
coarsens, which is survivable; a stack trace per request is not.

**Returns**:

  A ZoneInfo for Europe/London, or datetime.UTC when zone data is unavailable.

<a id="app.local_dt"></a>

### local\_dt

```python
def local_dt(epoch_s)
```

Epoch seconds as a Europe/London datetime.

Every temporal grain -- both hour_band and day_type -- is derived from this and never from UTC.

**Arguments**:

- `epoch_s` - Seconds since the Unix epoch.
  

**Returns**:

  A timezone-aware datetime in Europe/London.

<a id="app.day_type"></a>

### day\_type

```python
def day_type(dt)
```

'weekday' | 'sat' | 'sun' from a LOCAL datetime.

Saturday and Sunday operation differs from Mon-Fri in both frequency and
running time, so they are fitted separately. Bank holidays run a Sunday-ish
service and are deliberately NOT handled -- they file as 'weekday'. Building
a holiday calendar is out of scope; this is the documented gap.

**Arguments**:

- `dt` - A local datetime.
  

**Returns**:

  'weekday', 'sat' or 'sun'.

<a id="app.hour_band"></a>

### hour\_band

```python
def hour_band(dt)
```

Bucket a LOCAL hour into the bands the dbt model groups on.

The marts carry '*' fallback rows, so a disagreement over the boundaries degrades to a coarser
model rather than to no model at all.

**Arguments**:

- `dt` - A local datetime.
  

**Returns**:

  The hour-band label the dbt model groups on.

<a id="app.pick_model"></a>

### pick\_model

```python
def pick_model(models, operator, line, direction, day, band)
```

Best-matching eta_models row, most specific grain first.

Grain is (operator, line, direction, day_type, hour_band); each fallback
row is refitted at its own grain rather than averaged.

Operator, direction and day are all allowed to miss. operator is NULL on
every ping before 2026-09-12, so the operator-specific rungs are skipped
entirely (not merely tried and missed) whenever the live bus has none --
trying them anyway would only ever hit a '*'-operator row, uselessly
labelled as if it were operator-specific. The marts' 'inbound'/'outbound'
is inferred from journey geometry over history that predates the schedule
columns, so it need not agree with the live feed; and while the marts are
being rebuilt they may carry no day_type at all, in which case every row
is filed under '*'. Any mismatch must fall through to a coarser grain
rather than yield no prediction.

**Arguments**:

- `models` - Mapping of grain keys to eta_models rows, as loaded from the marts.
- `operator` - Operator code (NOC), or None.
- `line` - Published service number.
- `direction` - Journey direction, or None.
- `day` - Day type from day_type().
- `band` - Hour band from hour_band().
  

**Returns**:

  The most specific matching row, or None when no grain matches.

<a id="app.display_name"></a>

### display\_name

```python
def display_name(value)
```

The feed hands us names with underscores for spaces (`Stockport_Interchange`).

Only swap them back when the string has no real spaces, so a genuinely underscored name
survives.

**Arguments**:

- `value` - Stop or place name as supplied by the feed; may be None.
  

**Returns**:

  The name with underscores turned back into spaces where appropriate.

<a id="app.iso_from_epoch_s"></a>

### iso\_from\_epoch\_s

```python
def iso_from_epoch_s(seconds)
```

Format epoch seconds as an ISO 8601 UTC string ending in Z.

**Arguments**:

- `seconds` - Seconds since the Unix epoch, or None.
  

**Returns**:

  The ISO string, or None when seconds is None.

<a id="app.iso_from_millis"></a>

### iso\_from\_millis

```python
def iso_from_millis(millis)
```

Format epoch milliseconds as an ISO 8601 UTC string ending in Z.

**Arguments**:

- `millis` - Milliseconds since the Unix epoch, or None.
  

**Returns**:

  The ISO string, or None when millis is None or not numeric.

<a id="app.epoch_from_qdb_timestamp"></a>

### epoch\_from\_qdb\_timestamp

```python
def epoch_from_qdb_timestamp(value)
```

QuestDB hands back '2026-09-12T09:03:00.000000Z'.

**Arguments**:

- `value` - QuestDB timestamp string, or falsy.
  

**Returns**:

  Epoch seconds as a float, or None when value is empty or unparseable.

<a id="app.get_marts"></a>

### get\_marts

```python
def get_marts()
```

Cached marts, reloaded when the file's mtime moves.

The mtime is checked at most once a minute.

**Returns**:

  The cached marts dict; its "ok" key says whether the data is usable.

<a id="app.qdb_exec"></a>

### qdb\_exec

```python
def qdb_exec(sql)
```

Run a SQL query against QuestDB over HTTP /exec.

**Arguments**:

- `sql` - SQL text; the caller is responsible for validating anything interpolated.
  

**Returns**:

  The decoded JSON response.
  

**Raises**:

- `urllib.error.URLError` - If QuestDB is unreachable or answers with an HTTP
  error.

<a id="app.qdb_rows"></a>

### qdb\_rows

```python
def qdb_rows(sql)
```

Run a query and return rows as dicts keyed by column name.

**Arguments**:

- `sql` - SQL text, as for qdb_exec.
  

**Returns**:

  List of dicts keyed by column name.
  

**Raises**:

- `urllib.error.URLError` - If QuestDB is unreachable or answers with an HTTP
  error.

<a id="app.available_columns"></a>

### available\_columns

```python
def available_columns()
```

Return the enriched columns that exist on bus_positions.

The enriched columns are created lazily by the ingestor's ILP writes, so
select only what actually exists or QuestDB 400s the whole query.

**Returns**:

  Set of column names present on bus_positions.

<a id="app.select_columns"></a>

### select\_columns

```python
def select_columns()
```

Columns to select from bus_positions: the base set plus those extras that exist.

**Returns**:

  List of column names.

<a id="app.live_buses"></a>

### live\_buses

```python
def live_buses(bus_id=None, operator=None)
```

Newest row per VEHICLE inside the freshness window.

A vehicle is (bus_id, operator), not bus_id: operators mint their own
references, so '154' is a different physical bus for SWWD in Hampshire, TFCN
in Cornwall and GPLM near Luton. Partitioning by bus_id alone collapsed those
namesakes into one and hid about 3% of the live fleet (474 of 16,580 vehicles
on 2026-09-20). Measured on the live table the two-column form costs the same
(~0.1 s), so there is no reason to keep the wrong key.

operator narrows a single-bus lookup to one namesake. It is validated by the
caller against BUS_ID_RE, like bus_id, because it is interpolated into SQL.

**Arguments**:

- `bus_id` - Restrict to this bus_id; validated against BUS_ID_RE by the caller.
- `operator` - Restrict to this operator (NOC); validated by the caller.
  

**Returns**:

  List of row dicts, newest per vehicle.

<a id="app.bus_history"></a>

### bus\_history

```python
def bus_history(bus_id, operator=None)
```

Recent pings of one bus, newest first.

**Arguments**:

- `bus_id` - Bus identifier; validated against BUS_ID_RE by the caller.
- `operator` - Operator (NOC) to disambiguate namesakes; validated by the caller.
  

**Returns**:

  List of dicts with lat, lon and timestamp (at most 500).

<a id="app.schedule_epoch_s"></a>

### schedule\_epoch\_s

```python
def schedule_epoch_s(millis, now_s, max_age_s, max_ahead_s)
```

Epoch seconds for an aimed time, or None if it is too stale to trust.

The feed keeps republishing a vehicle's last journey until it logs on to the
next one, so an overnight bus advertises yesterday's timetable. Those records
are not late buses, they are dead records.

**Arguments**:

- `millis` - Aimed time, epoch milliseconds, or None.
- `now_s` - Current epoch seconds.
- `max_age_s` - Oldest acceptable age of the aimed time, seconds.
- `max_ahead_s` - Furthest acceptable distance into the future, seconds.
  

**Returns**:

  The aimed time in epoch seconds, or None when missing or out of the window.

<a id="app.nearest_stop"></a>

### nearest\_stop

```python
def nearest_stop(stops, lat, lon)
```

Closest stop of this route to a position, within STOP_MATCH_KM.

A plain scan: a route has tens of stops, and this runs only for the buses
the distance method could not rate, so no spatial index is worth holding.

**Arguments**:

- `stops` - Stop dicts of the route, with lat and lon.
- `lat` - Vehicle latitude.
- `lon` - Vehicle longitude.
  

**Returns**:

  A tuple (stop, distance_km); (None, None) when no stop is within
  STOP_MATCH_KM.

<a id="app.progress_delay"></a>

### progress\_delay

```python
def progress_delay(row, stops, now_s, lat, lon)
```

Delay from schedule adherence rather than from the ETA regression.

The feed gives aimed times for the journey's origin and destination only --
there are no per-stop scheduled times anywhere in SIRI-VM. So we take the
SHAPE of the route from the published timetable (each stop's scheduled
seconds from the start of its pattern) and stretch it onto THIS
journey's advertised timetable:

fraction  = (here - origin) / (destination - origin)   [observed shape]
expected  = fraction * (aimed_arrival - aimed_departure)
delay     = (now - aimed_departure) - expected

Anchoring on the origin stop rather than on the journey's first ping matters:
secs_from_origin is measured from wherever tracking happened to begin, so
using it raw hands every bus its own depot-to-terminus lag as permanent
lateness. Expressed as a fraction those anchors cancel, and at the last stop
the whole thing collapses to plain actual-arrival vs aimed-arrival.

Errors in absolute speed cancel too, which is the point: the ETA
regression's in-sample MAE is 6-11 minutes, wider than the 6-minute on-time
window, so it cannot carry the RAG classification on its own.

**Arguments**:

- `row` - Live vehicle row.
- `stops` - Route stop dicts, in order, with scheduled seconds.
- `now_s` - Current epoch seconds.
- `lat` - Vehicle latitude.
- `lon` - Vehicle longitude.
  

**Returns**:

  A tuple (delay_s, matched_stop); (None, None) when the delay cannot be judged.

<a id="app.geometric_progress_delay"></a>

### geometric\_progress\_delay

```python
def geometric_progress_delay(row, marts, now_s)
```

Estimate delay from plain distance covered between origin and destination.

Same idea as progress_delay, but measuring progress as plain distance
covered between the origin and destination stops.

progress_delay needs the bus to be near a stop the marts already know on
this line, which holds for only a couple of hundred vehicles. This needs
nothing but the two terminus coordinates, which the feed gives as ATCO codes
on almost every vehicle, so it covers most of the fleet. It assumes progress
is uniform in distance rather than in time -- crude in town centres -- but it
is unbiased at both ends, and at the destination it collapses to exactly the
arrival-vs-timetable comparison.

**Arguments**:

- `row` - Live vehicle row.
- `marts` - Marts dict (unused when the stops come from GTFS).
- `now_s` - Current epoch seconds.
  

**Returns**:

  Delay in seconds, or None when it cannot be judged.

<a id="app.position_is_live"></a>

### position\_is\_live

```python
def position_is_live(row, now_s)
```

Is this vehicle actually reporting, or is the feed replaying a dead record?

Freshness has to be judged on the SIRI RecordedAtTime, not on when we
ingested the row: a stale record is re-served to us every 60 seconds and so
always looks new. Vehicles with no recorded_at at all (pre-patch rows, and
operators that omit it) are let through -- the five-minute ingest window is
the only freshness signal available for those.

**Arguments**:

- `row` - Live vehicle row.
- `now_s` - Current epoch seconds.
  

**Returns**:

  True when the position is fresh enough to trust, False for a replayed dead
  record.

<a id="app.pinned_trip"></a>

### pinned\_trip

```python
def pinned_trip(row)
```

Return the GTFS trip this row's journey unambiguously pins to, or None.

The trip is the one the journey pins to (ROADMAP.md
1.13.0 / ADR-0008 Phase 2), or None. A thin wrapper over gtfs.pin_trip
keyed the same way journey_key() keys a journey, so a repeated call for
the same journey is a cache hit, not a re-query.

**Arguments**:

- `row` - Live vehicle row.
  

**Returns**:

  The pinned trip dict from gtfs.pin_trip, or None.

<a id="app.pinned_stop_arrival"></a>

### pinned\_stop\_arrival

```python
def pinned_stop_arrival(pin, atco)
```

Return a pinned trip's wall-clock arrival epoch at one stop, or None.

This is the pinned trip's real wall-clock arrival epoch at one stop, or None
if the stop is not on it (should not happen for origin/dest, which is how
the pin was found, but a defensive lookup costs nothing).

**Arguments**:

- `pin` - Pinned trip dict from pinned_trip().
- `atco` - ATCO code of the stop.
  

**Returns**:

  Arrival in epoch seconds, or None when the stop is not on the trip.

<a id="app.pinned_eta"></a>

### pinned\_eta

```python
def pinned_eta(row, now_s)
```

Return (predicted_epoch_s, eta_iso) from a pinned trip's own timetable.

(predicted_epoch_s, eta_iso) from a pinned trip's own scheduled
arrival at this journey's destination, or (None, None) if the trip does
not pin. This is the timetable ITSELF, not a delay adjustment -- delay
still comes from progress_delay/geometric_progress_delay exactly as
before; a pinned vehicle still gets progress-based lateness on top,
same as any other. The reasonableness window is the same one
schedule_epoch_s applies to the feed's own aimed times, since a pinned
arrival is exactly that sort of value.

**Arguments**:

- `row` - Live vehicle row.
- `now_s` - Current epoch seconds.
  

**Returns**:

  A tuple (predicted_epoch_s, eta_iso); (None, None) when the trip does not pin.

<a id="app.predict"></a>

### predict

```python
def predict(row, marts, now_s)
```

Return (properties-fragment, model, predicted_epoch_s).

**Arguments**:

- `row` - Live vehicle row.
- `marts` - Marts dict from get_marts().
- `now_s` - Current epoch seconds.
  

**Returns**:

  A tuple (properties fragment, model, predicted_epoch_s).

<a id="app.bus_properties"></a>

### bus\_properties

```python
def bus_properties(row, marts, now_s)
```

GeoJSON properties for one bus: identity, names, times, ETA and status.

**Arguments**:

- `row` - Live vehicle row.
- `marts` - Marts dict from get_marts().
- `now_s` - Current epoch seconds.
  

**Returns**:

  A tuple (properties dict, model, predicted_epoch_s).

<a id="app.prefetch_timetable"></a>

### prefetch\_timetable

```python
def prefetch_timetable(rows, sync_seed=False)
```

Load, in two queries, every stop position and route pattern this poll will ask for.

Left to the per-bus lookups it would be thousands of queries on the first poll after a GTFS
refresh. Then feed each ambiguous bus's latest ping to the pattern tiebreak (sync_seed:
backfill a new trail before returning, for a single-bus request, instead of in the background).

**Arguments**:

- `rows` - Live vehicle rows of one poll.
- `sync_seed` - True to backfill new trails before returning.
  

**Returns**:

  None. Caches are filled as a side effect.

<a id="app.SEED_MINUTES"></a>

#### SEED\_MINUTES

how far back a new trail is backfilled (ADR-0008 (h)(i))

<a id="app.SEED_PINGS"></a>

#### SEED\_PINGS

...from at most this many evenly spaced pings, to bound CPU

<a id="app.SEED_CHUNK"></a>

#### SEED\_CHUNK

bus ids per QuestDB query, to keep the URL short

<a id="app.journey_key"></a>

### journey\_key

```python
def journey_key(row)
```

Key identifying a vehicle's current journey.

**Arguments**:

- `row` - Live vehicle row.
  

**Returns**:

  A tuple (journey_ref, origin_ref, origin_aimed_dep).

<a id="app.observe_trails"></a>

### observe\_trails

```python
def observe_trails(rows, sync_seed=False)
```

Score the latest ping of every multi-candidate bus and queue trail backfills.

Score the latest ping of every bus with more than one candidate pattern,
and queue the ones without a trail for backfilling.

**Arguments**:

- `rows` - Live vehicle rows of one poll.
- `sync_seed` - True to backfill new trails before returning.
  

**Returns**:

  None.

<a id="app.seed_trails"></a>

### seed\_trails

```python
def seed_trails(items, gen)
```

Backfill trails from the last SEED_MINUTES of QuestDB pings.

Backfill trails from the last SEED_MINUTES of QuestDB pings, so an API
restart or a newly seen vehicle does not start with no evidence. Failure
just leaves the prior in charge.

**Arguments**:

- `items` - Tuples (vehicle, journey, candidates) needing backfill.
- `gen` - GTFS build generation the candidates belong to.
  

**Returns**:

  None. Failures are swallowed.

<a id="app.stop_lookup"></a>

### stop\_lookup

```python
def stop_lookup(atco)
```

A stop's name and position from its ATCO code, from NaPTAN via the GTFS database.

National, so London resolves too.

**Arguments**:

- `atco` - ATCO code, or falsy.
  

**Returns**:

  Stop dict, or None when unknown or atco is falsy.

<a id="app.route_for"></a>

### route\_for

```python
def route_for(row)
```

(stop sequence, how it was found) for the route this bus is working.

1. A timetable pattern that runs exactly origin -> destination. When several
do, the trail tiebreak (tiebreak.py) picks one if the evidence is clear;
otherwise the most-run one, reported as 'gtfs_pattern_unresolved'.
2. The line's primary pattern, which is enough to name a next stop but is
not pinned to this journey ('gtfs_line').
3. Nothing ([], 'none'): a line the timetable lacks, which today means
London. Callers must not trust a stop sequence that lacks the bus's own
origin and destination; progress_delay checks.

**Arguments**:

- `row` - Live vehicle row.
  

**Returns**:

  A tuple (stops, source) where source is e.g. 'gtfs_pattern',
  'gtfs_pattern_unresolved', 'gtfs_line' or 'none'.

<a id="app.find_last_stop"></a>

### find\_last\_stop

```python
def find_last_stop(pings, stops)
```

Newest ping that sits within 100 m of a stop on this line.

**Arguments**:

- `pings` - Pings, newest first, each with lat, lon and timestamp.
- `stops` - Stop dicts of the route.
  

**Returns**:

  A tuple (stop, timestamp); (None, None) when no ping is near a stop.

<a id="app.buses"></a>

### buses

```python
@buses_blp.route('/buses')
@buses_blp.doc(
    summary='Every live bus, as GeoJSON',
    description=
    'Rebuilt at most once every BUSES_TTL seconds; concurrent requests '
    'inside that window all get the same cached body. Not schema-validated '
    'per request -- see schemas.BusesResponseSchema and its own docstring '
    'for why: this is a pre-rendered cached string, and running it through '
    'marshmallow on every request would defeat the point of caching it.',
    responses={
        200: {
            'description': 'FeatureCollection of bus Point features.',
            'content': {
                'application/json': {
                    'schema': schemas.BusesResponseSchema
                }
            }
        },
        503: {
            'description': 'No cached body yet and the upstream build failed.',
            'content': {
                'application/json': {
                    'schema': schemas.BusesResponseSchema
                }
            }
        },
    })
def buses()
```

GET /buses: the live fleet as a cached GeoJSON FeatureCollection.

**Returns**:

  The cached JSON response, or a 503 with an empty FeatureCollection when it
  cannot be built.

<a id="app.bus_detail"></a>

### bus\_detail

```python
@buses_blp.route('/bus/<bus_id>')
@buses_blp.arguments(schemas.BusDetailQuerySchema, location='query')
@buses_blp.response(200, schemas.BusDetailSchema)
@buses_blp.alt_response(400,
                        schema=schemas.ApiErrorSchema,
                        description="Invalid bus_id or operator.")
@buses_blp.alt_response(404,
                        schema=schemas.ApiErrorSchema,
                        description="bus_id not currently live.")
@buses_blp.alt_response(503,
                        schema=schemas.ApiErrorSchema,
                        description='QuestDB unavailable.')
def bus_detail(args, bus_id)
```

One vehicle.

bus_id alone is not unique across operators, so an optional ?operator=<NOC> selects one
namesake. Old clients send only bus_id and keep working: with namesakes and no operator the
FRESHEST one is returned (deterministic, unlike the arbitrary first row this used to hand back)
and the payload lists who else answers to that id in `same_bus_id`.

**Arguments**:

- `args` - Validated query arguments; may carry "operator".
- `bus_id` - Bus identifier from the URL path.
  

**Returns**:

  The bus detail dict, or a tuple of a JSON error and status 400 (invalid id),
  404 (not live) or 503 (upstream unavailable).

<a id="app.road_snapped_geometry"></a>

### road\_snapped\_geometry

```python
def road_snapped_geometry(stops)
```

Return [[lon, lat], ...] following real roads through the stops, or None.

[[lon, lat], ...] following real roads through these stops in order,
via the self-hosted OSRM car profile (overview=full for the detailed
shape, not OSRM's default simplified one). None on any failure --
unreachable OSRM, a stop pair with no drivable path, a timeout -- so the
caller can fall back to the straight stop-to-stop line rather than
breaking the map.

**Arguments**:

- `stops` - Stop dicts in order, with lat and lon.
  

**Returns**:

  List of [lon, lat] coordinates, or None on any failure.

<a id="app.route_stops_for_line"></a>

### route\_stops\_for\_line

```python
def route_stops_for_line(operator, line, direction)
```

Return the stop sequence plus how it was resolved, for /api/route.

Stop sequence plus how it was resolved, for /api/route:
the timetable's primary pattern. Returns (stops, operator, direction, day_type, source); the
day_type is always None (patterns are not day-specific) and is kept only so the response shape
does not change for clients.

**Arguments**:

- `operator` - Operator code (NOC), or None.
- `line` - Published service number.
- `direction` - Requested direction, or None.
  

**Returns**:

  A tuple (stops, operator, direction, day_type, source).

<a id="app.route"></a>

### route

```python
@buses_blp.route('/route')
@buses_blp.arguments(schemas.RouteQuerySchema, location='query')
@buses_blp.response(200, schemas.RouteResponseSchema)
@buses_blp.alt_response(
    400,
    schema=schemas.ApiErrorSchema,
    description='Invalid line, direction, day_type or operator.')
def route(args)
```

Return GeoJSON for drawing a line on the map.

GeoJSON for drawing a line on the map:
its stop sequence as a dotted LineString, the stops themselves, and the buses currently working
it.

`line` is user-controlled via the query string and is interpolated into
nothing here -- stop lookup is entirely in-memory against the marts, and
the live-bus filter compares in Python -- but it is still whitelisted
up front, same discipline as bus_id, because it flows into the response
unexamined otherwise.

**Arguments**:

- `args` - Validated query arguments: line, direction, day_type, operator.
  

**Returns**:

  The FeatureCollection dict, or a tuple of a JSON error and status 400 for an
  invalid parameter.

<a id="app.search"></a>

### search

```python
@search_blp.route('/search')
@search_blp.arguments(schemas.SearchQuerySchema, location='query')
@search_blp.response(200, schemas.SearchResponseSchema)
def search(args)
```

GET /search: localities, lines and bus ids matching free text.

**Arguments**:

- `args` - Validated query arguments; "q" is the search text.
  

**Returns**:

  Dict with query, matches, lines and bus_ids; empty lists for under two
  normalised characters.

<a id="app.marts_age_hours"></a>

### marts\_age\_hours

```python
def marts_age_hours(marts)
```

Age of the marts build in hours.

**Arguments**:

- `marts` - Marts dict from get_marts().
  

**Returns**:

  Hours since built_at rounded to two places, or None when unknown.

<a id="app.model_info"></a>

### model\_info

```python
@system_blp.route('/model-info')
@system_blp.response(200, schemas.ModelInfoResponseSchema)
def model_info()
```

GET /model-info: freshness and state of the dbt marts.

**Returns**:

  Dict describing the marts state, age and staleness.

<a id="app.metrics_summary"></a>

### metrics\_summary

```python
@system_blp.route('/metrics/summary')
@system_blp.response(200, schemas.MetricsSummaryResponseSchema)
def metrics_summary()
```

The five headline numbers from the dashboard's top row.

Each is its own qdb_exec call and fails independently -- a QuestDB hiccup on one query
shouldn't blank out the four that would have worked, the same reasoning bus_detail() already
applies to last_stop/next_stop.

**Returns**:

  Dict of the five headline numbers (each may be null), independently of each
  other.

<a id="app.metrics_punctuality"></a>

### metrics\_punctuality

```python
@system_blp.route('/metrics/punctuality')
@system_blp.response(200, schemas.MetricsPunctualityResponseSchema)
@system_blp.alt_response(503,
                         schema=schemas.ApiErrorSchema,
                         description='QuestDB unavailable.')
def metrics_punctuality()
```

Return system-wide punctuality, ETA model fit and the worst lines.

System-wide on-time %, delay and ETA model fit, plus the worst lines
table -- the same rollup dbt/publish_punctuality_summary.py writes into
QuestDB and the same queries the dashboard's 'Punctuality & ETA models'
row runs. The per-line rollup is a volume-weighted approximation from
grain-level medians, not a true per-journey percentage -- see that
script's own docstring for what it does and doesn't measure.

**Returns**:

  Dict with system, model_fit and worst_lines, or a tuple of a JSON error and
  status 503 when QuestDB is unavailable.

<a id="gtfs"></a>

## gtfs

Read-only lookups against the served GTFS database (gtfs/fetch.py builds it).

Replaces the line_stops / line_localities marts (ADR-0008). Three rules:

* Never hold a connection. Every query opens the file read-only and closes it,
  like the marts loader, so a weekly refresh swapping the file is never blocked.
* Nothing is loaded up front. The database is 1.1 GB and gtfs_route_stops alone
  is 1.5M rows; loading it into Python is exactly what made the API need 6 GB
  for line_stops. Lookups are lazy and cached per key.
* Every cache is dropped when the file's mtime moves, because pattern_id is only
  stable within one build. A missing or unreadable file degrades to "no answer",
  never an exception.

Coverage caveat: BODS GTFS has no TfL (TFLO) timetable, so routes and line
localities are empty for London. Stop coordinates come from NaPTAN, which does
cover London, so the destination-distance ETA and geometric delay still work
there.

Must never write to the GTFS database, hold a connection open between calls,
or raise to a caller because the file is missing or unreadable. Building the
database is gtfs/fetch.py's job, not this module's.

<a id="gtfs.GTFS_TTL"></a>

#### GTFS\_TTL

only stat the file once a minute

<a id="gtfs.name_fn"></a>

#### name\_fn

noqa: E731

<a id="gtfs.generation"></a>

#### generation

bumps when the file changes; pattern ids die with it

<a id="gtfs.available"></a>

### available

```python
def available()
```

Whether the GTFS database is present and the duckdb driver importable.

**Returns**:

  True when lookups can be answered, False when every lookup will return no
  answer.

<a id="gtfs.prime"></a>

### prime

```python
def prime(atcos)
```

Fill the stop cache for many ATCO codes in one query (one poll's worth).

**Arguments**:

- `atcos` - Iterable of ATCO codes; falsy and already cached codes are skipped.
  

**Returns**:

  None. The stop cache is filled in place; unknown codes are cached as None.

<a id="gtfs.stop"></a>

### stop

```python
def stop(atco)
```

{'atco','name','locality','lat','lon',...} for an ATCO code, or None.

**Arguments**:

- `atco` - ATCO code of the stop; falsy returns None.
  

**Returns**:

  Stop dict with atco, name, locality, lat and lon, or None when unknown.

<a id="gtfs.prime_routes"></a>

### prime\_routes

```python
def prime_routes(keys)
```

Batch-load candidates for the keys of one poll.

The keys are (operator, line, origin_ref, dest_ref); keys already cached, or missing a
field, are skipped.

**Arguments**:

- `keys` - Iterable of (operator, line, origin_ref, dest_ref) tuples.
  

**Returns**:

  None. The candidate cache is filled in place.

<a id="gtfs.candidates"></a>

### candidates

```python
def candidates(operator, line, origin_ref, dest_ref)
```

List every timetable pattern of this line that runs origin_ref -> dest_ref.

Most-run first (trip_share desc, then lowest pattern_id, so the order is
total). [] when the line is not in the timetable or no pattern matches the
endpoints. Each: {pattern_id, direction, trip_share, is_circular, stops}.

**Arguments**:

- `operator` - Operator code (NOC).
- `line` - Published service number.
- `origin_ref` - ATCO code of the journey origin.
- `dest_ref` - ATCO code of the journey destination.
  

**Returns**:

  List of candidate pattern dicts, most-run first; empty when none match or any
  argument is falsy.

<a id="gtfs.prime_pins"></a>

### prime\_pins

```python
def prime_pins(keys)
```

Batch-load trip pins for the given keys.

The keys are (operator, line, origin_ref, aimed_dep_ms); keys already cached, or missing
a field, are skipped.

**Arguments**:

- `keys` - Iterable of (operator, line, origin_ref, aimed_dep_ms) tuples.
  

**Returns**:

  None. The pin cache is filled in place.

<a id="gtfs.pin_trip"></a>

### pin\_trip

```python
def pin_trip(operator, line, origin_ref, aimed_dep_ms)
```

Return the one GTFS trip a vehicle's journey pins to, or None.

Result is {'trip_id', 'anchor_epoch_s', 'stops':
[{'atco','seq','arr_secs', 'dep_secs'}, ...]} for the one GTFS trip this vehicle's current
journey unambiguously matches by (operator, line, origin stop, exact aimed departure) -- or
None if it does not pin (no match, more than one match, or a missing field). See the module
note above for why exact-match only. anchor_epoch_s + any stop's arr_secs/dep_secs is that
stop's real wall-clock epoch (see _load_pins for why a plain "local midnight" does not always
work). stops is the WHOLE trip, ordered by stop_sequence.

**Arguments**:

- `operator` - Operator code (NOC).
- `line` - Published service number.
- `origin_ref` - ATCO code of the journey origin.
- `aimed_dep_ms` - Aimed departure from the origin, epoch milliseconds.
  

**Returns**:

  The pinned trip dict described above, or None when the journey does not pin.

<a id="gtfs.prime_lines"></a>

### prime\_lines

```python
def prime_lines(keys)
```

Batch-load line routes for (operator, line, direction) keys.

Keys already cached, or without an operator or line, are skipped.

**Arguments**:

- `keys` - Iterable of (operator, line, direction) tuples.
  

**Returns**:

  None. The line cache is filled in place.

<a id="gtfs.line_route"></a>

### line\_route

```python
def line_route(operator, line, direction=None)
```

Return one representative route for a line, for the map's route overlay.

Shape: {stops, operator, direction, source}, or None. Chooses among the primary patterns: the
requested direction if it exists, else outbound, else any; with no operator, the one carrying
the most trips.

**Arguments**:

- `operator` - Operator code (NOC), or falsy to pick across operators.
- `line` - Published service number; falsy returns None.
- `direction` - Preferred direction, or None.
  

**Returns**:

  Dict with stops, operator, direction and source, or None when the line has no
  route.

<a id="gtfs.line_localities"></a>

### line\_localities

```python
def line_localities(norms)
```

{locality_norm: {line, ...}} for the search box. Empty for TfL.

**Arguments**:

- `norms` - Iterable of normalised locality names; falsy entries are ignored.
  

**Returns**:

  Dict mapping each locality_norm that has lines to the set of line names.

<a id="schemas"></a>

## schemas

marshmallow schemas for flask-smorest, one per HTTP response/query shape.

These exist purely to generate an accurate OpenAPI spec + Swagger UI from
the real route decorators (see app.py's Api/Blueprint setup) -- they do not
change any response's actual JSON. Two deliberate exceptions to "model every
field exactly":

- GeoJSON `properties` on a Feature varies by `kind` (route/stop/bus) --
  BusFeaturePropertiesSchema documents the richest (bus) shape; route/stop
  features carry a strict SUBSET of it. Modelling all three as one big
  oneOf was judged not worth the complexity for an internal API with two
  known consumers.
- /buses itself is intentionally NOT schema-validated at response time (see
  app.py) -- it serves a pre-rendered, cached JSON string for performance,
  and running that through marshmallow's dump on every request would defeat
  the point of caching it. Its shape is documented via BusesResponseSchema
  for Swagger UI only.

Must never be used to change a response's actual JSON, and must never validate
or serialise /buses at request time.

<a id="schemas.ApiErrorSchema"></a>

### ApiErrorSchema

```python
class ApiErrorSchema(Schema)
```

Error body, {"error": <message>}, returned with a 4xx or 5xx status.

<a id="schemas.BusFeaturePropertiesSchema"></a>

### BusFeaturePropertiesSchema

```python
class BusFeaturePropertiesSchema(Schema)
```

Schema of bus_properties()'s exact field set.

This is what every 'bus' Feature on /buses and /route carries, and the base of
BusDetailSchema below.

<a id="schemas.ModelFitSchema"></a>

### ModelFitSchema

```python
class ModelFitSchema(Schema)
```

ETA regression fit behind a prediction: slope, intercept, R2, n_obs.

<a id="schemas.StopRefSchema"></a>

### StopRefSchema

```python
class StopRefSchema(Schema)
```

A stop reference: name, locality, ATCO code and position.

<a id="schemas.LastStopSchema"></a>

### LastStopSchema

```python
class LastStopSchema(StopRefSchema)
```

The last stop a bus passed, with the time it was there.

<a id="schemas.NextStopSchema"></a>

### NextStopSchema

```python
class NextStopSchema(StopRefSchema)
```

The next stop on a bus's route, with its ETA and aimed time.

<a id="schemas.NamesakeSchema"></a>

### NamesakeSchema

```python
class NamesakeSchema(Schema)
```

Another operator's vehicle that shares a bus_id: operator and line.

<a id="schemas.BusDetailSchema"></a>

### BusDetailSchema

```python
class BusDetailSchema(BusFeaturePropertiesSchema)
```

Schema of the GET /bus/<bus_id> response.

Extends the properties every bus Feature carries
with the position and route detail only a single-bus lookup computes.

<a id="schemas.BusDetailQuerySchema"></a>

### BusDetailQuerySchema

```python
class BusDetailQuerySchema(Schema)
```

Query string of GET /bus/<bus_id>.

<a id="schemas.GeometrySchema"></a>

### GeometrySchema

```python
class GeometrySchema(Schema)
```

A GeoJSON geometry, loosely typed: a Point or a LineString.

<a id="schemas.FeatureSchema"></a>

### FeatureSchema

```python
class FeatureSchema(Schema)
```

A GeoJSON Feature whose properties shape depends on its kind.

<a id="schemas.BusesResponseSchema"></a>

### BusesResponseSchema

```python
class BusesResponseSchema(Schema)
```

Documents /buses; not used to serialize it -- see the module docstring.

<a id="schemas.RouteResponseSchema"></a>

### RouteResponseSchema

```python
class RouteResponseSchema(Schema)
```

GeoJSON FeatureCollection returned by GET /route.

<a id="schemas.RouteQuerySchema"></a>

### RouteQuerySchema

```python
class RouteQuerySchema(Schema)
```

Query string of GET /route.

<a id="schemas.SearchMatchSchema"></a>

### SearchMatchSchema

```python
class SearchMatchSchema(Schema)
```

One locality match in a search result.

<a id="schemas.SearchResponseSchema"></a>

### SearchResponseSchema

```python
class SearchResponseSchema(Schema)
```

Response of GET /search: matching localities, lines and bus ids.

<a id="schemas.SearchQuerySchema"></a>

### SearchQuerySchema

```python
class SearchQuerySchema(Schema)
```

Query string of GET /search.

<a id="schemas.ModelInfoResponseSchema"></a>

### ModelInfoResponseSchema

```python
class ModelInfoResponseSchema(Schema)
```

Response of GET /model-info: freshness and state of the dbt marts.

<a id="schemas.MetricsSummaryResponseSchema"></a>

### MetricsSummaryResponseSchema

```python
class MetricsSummaryResponseSchema(Schema)
```

Response of GET /metrics/summary: the dashboard's five headline numbers.

<a id="schemas.PunctualitySystemSchema"></a>

### PunctualitySystemSchema

```python
class PunctualitySystemSchema(Schema)
```

System-wide on-time percentage and median delay.

<a id="schemas.PunctualityModelFitSchema"></a>

### PunctualityModelFitSchema

```python
class PunctualityModelFitSchema(Schema)
```

Aggregate quality of the ETA models: R squared, weak-fit share and speed.

<a id="schemas.WorstLineSchema"></a>

### WorstLineSchema

```python
class WorstLineSchema(Schema)
```

One row of the worst-lines punctuality table.

<a id="schemas.MetricsPunctualityResponseSchema"></a>

### MetricsPunctualityResponseSchema

```python
class MetricsPunctualityResponseSchema(Schema)
```

Response of GET /metrics/punctuality.

<a id="tiebreak"></a>

## tiebreak

Choose between timetable patterns that share a bus's origin and destination.

ADR-0008 (h)(i). Endpoints leave several candidates for about 18% of buses (a
diversion, a school-day variant), and where the bus is right now does not tell
them apart: the variants share most stops. Evidence accumulated over the whole
journey does, modestly: the running mean distance from each ping to each
candidate's stop-to-stop polyline. Measured: the prior alone is right 72.4% of
the time, this hybrid 77.8%.

The rule (all numbers from the ADR, tuned on one snapshot, so treat them as
provisional):
  * each ping adds min(distance to the polyline, CAP_M) to that candidate's sum;
  * if the best candidate's mean leads the runner-up's by LEAD_M or more, and is
    itself under INFORMATIVE_M, take it;
  * otherwise take the prior -- the pattern most trips run, then the lowest
    pattern_id (the order gtfs.candidates already returns) -- and say so, so
    callers can show the result as unresolved.

State is a per-vehicle running sum, not a list of pings, and lives in memory: it
is lost on restart and re-seeded from QuestDB by app.py. It resets when the
journey changes or the GTFS file is swapped (pattern ids only mean something
within one build).

Must never touch a database, the network or the filesystem: it holds only
in-memory per-vehicle state, and callers (app.py) feed it pings and read the
result. It must never raise on missing coordinates; those pings are skipped.

<a id="tiebreak.CAP_M"></a>

#### CAP\_M

one wild ping cannot dominate a mean

<a id="tiebreak.LEAD_M"></a>

#### LEAD\_M

required lead of the best candidate over the runner-up

<a id="tiebreak.INFORMATIVE_M"></a>

#### INFORMATIVE\_M

straight stop-to-stop lines are this wrong by themselves

<a id="tiebreak.FORGET_S"></a>

#### FORGET\_S

drop a vehicle's trail this long after we last saw it

<a id="tiebreak.distance_m"></a>

### distance\_m

```python
def distance_m(lat, lon, stops)
```

Return the distance in metres from a point to the polyline through these stops.

Measured in a local flat
projection (fine at bus scale). inf for fewer than one usable stop.

**Arguments**:

- `lat` - Latitude of the ping, in degrees.
- `lon` - Longitude of the ping, in degrees.
- `stops` - Stop dicts with "lat" and "lon" keys, in route order; stops missing
  either are ignored.
  

**Returns**:

  Distance in metres, or math.inf when no stop has usable coordinates.

<a id="tiebreak.observe"></a>

### observe

```python
def observe(vehicle, journey, gen, cands, lat, lon, ts, seeding=False)
```

Add one ping to a vehicle's trail.

A live ping (seeding=False) ahead of a seeded one marks where live data starts, so seeding
never double-counts it.

**Arguments**:

- `vehicle` - Vehicle key the trail is stored under.
- `journey` - Journey identifier; a change resets the trail.
- `gen` - GTFS build generation; a change resets the trail.
- `cands` - Candidate patterns from gtfs.candidates, each with "direction",
  "pattern_id" and "stops".
- `lat` - Ping latitude, or None to skip the ping.
- `lon` - Ping longitude, or None to skip the ping.
- `ts` - Recorded-at timestamp string of the ping, or None.
- `seeding` - True when replaying history rather than a live ping.
  

**Returns**:

  None. The vehicle's trail is updated in place.

<a id="tiebreak.needs_seed"></a>

### needs\_seed

```python
def needs_seed(vehicle, journey, gen)
```

True once per (vehicle, journey): the caller should backfill its trail.

**Arguments**:

- `vehicle` - Vehicle key.
- `journey` - Journey identifier.
- `gen` - GTFS build generation.
  

**Returns**:

  True the first time it is asked for a vehicle with a live trail for this
  journey, otherwise False.

<a id="tiebreak.choose"></a>

### choose

```python
def choose(vehicle, journey, gen, cands)
```

(candidate, resolved). resolved is False when the prior was used.

**Arguments**:

- `vehicle` - Vehicle key.
- `journey` - Journey identifier.
- `gen` - GTFS build generation.
- `cands` - Candidate patterns, prior first (the order gtfs.candidates returns).
  

**Returns**:

  A tuple (candidate, resolved). resolved is True for a single candidate or a
  clear lead, False when the prior was used.

<a id="tiebreak.prune"></a>

### prune

```python
def prune(now=None)
```

Forget vehicles not seen for FORGET_S; at most once a minute.

**Arguments**:

- `now` - Epoch seconds to treat as the present; defaults to time.time().
  

**Returns**:

  None. Stale trails are dropped in place.

