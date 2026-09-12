{#
  The observed stop sequence for each (line, direction, day_type) -- what the
  API answers "what was the last stop" and "what is the next stop" from.

  There is no timetable in this project, so the sequence is reconstructed from
  behaviour. For every journey that passed within 120 m of a stop we know how
  many seconds into the journey that happened; the median of that over all
  journeys is a stable "this stop is about 14 minutes along the route", and
  ordering the stops by that median gives the route order.

  Median rather than mean, because one journey stuck in a road closure would
  otherwise reorder the route and produce confidently wrong next-stop answers.

  Two row sets are emitted: one per real day_type, and one at day_type = '*'
  fitted across all days, so a lookup always resolves even for a line that only
  runs Monday to Friday. Like mart_eta_model's fallbacks, the '*' set is
  rebuilt from the underlying observations rather than merged from the others.

  This mart matters more than it used to. The API refuses to estimate circular
  routes geometrically (origin and destination are the same stop, so the
  geometry says "you have arrived" for the whole loop), and falls back to this
  stop sequence instead -- so for those ~340 vehicles this table is the only
  thing that can produce an ETA at all. int_journey_shape has a dedicated
  direction rule for circular routes for that reason.

  HOW GOOD IS THE RESULT? Measured, not assumed: walk the published stops in
  seq order, sum the distance, divide by the route's actual length. A correct
  sequence scores ~1. This one scores 6-9x on a geographically coherent line
  and 200-480x on a line whose number is reused in several towns.

  The dominant cause is that journeys on one line start in many different
  places -- the 192 outbound has 24 distinct start locations, because short
  workings and part-route runs are pooled with full-length ones, and "seconds
  from origin" is not comparable across them. Restricting to full-length runs
  gets 9.0x to 6.1x; ordering by distance instead of time gets 9.0x to 8.6x.
  Neither is a fix, so neither is implemented -- machinery that makes the
  numbers look better without making them usable just buys false confidence.

  Treat seq as "roughly how far along the route", not as a next-stop oracle.
  The real fixes are journey_ref/DirectionRef from the post-2026-09-12 feed,
  operator in the mart grain, or simply reading the published timetable.

  Secondary artefact: 3-minute sampling quantises the median to multiples of
  180 s, so a run of consecutive stops ties. Stops on both sides of a road can
  both appear, because a bus passing one is within 120 m of the other.
#}

with matches as (

    select * from {{ ref('int_ping_stop_match') }}

),

first_visit_per_journey as (

    -- A journey usually matches the same stop on two or three consecutive
    -- pings (the bus is there for longer than one 3-minute bucket), and a
    -- circular route can pass the same stop twice. Collapse to one observation
    -- per (journey, stop): the earliest time the journey reached it.
    select
        journey_key,
        line,
        direction,
        day_type,
        atco_code,
        min(secs_from_origin) as secs_from_origin
    from matches
    group by journey_key, line, direction, day_type, atco_code

),

-- The same observations counted twice: once under their own day_type, and once
-- under '*'. Doing it as a union of the observation rows (rather than merging
-- finished aggregates) means the '*' medians are true medians over every
-- journey, not medians of medians.
observations as (

    select journey_key, line, direction, day_type, atco_code, secs_from_origin
    from first_visit_per_journey

    union all

    select journey_key, line, direction, '*' as day_type, atco_code, secs_from_origin
    from first_visit_per_journey

),

stop_identity as (

    -- Stop attributes, taken once rather than carried through the aggregation.
    select
        atco_code,
        mode(stop_name) as stop_name,
        mode(locality)  as locality,
        avg(stop_lat)   as lat,
        avg(stop_lon)   as lon
    from matches
    group by atco_code

),

by_grain as (

    select
        line,
        direction,
        day_type,
        atco_code,
        cast(median(secs_from_origin) as double)      as median_secs_from_origin,
        -- Only ever used to break ties in the ordering, never published.
        -- Pings are 3-minute buckets, so secs_from_origin is quantised to
        -- multiples of 180 and a run of consecutive stops shares one median.
        -- The mean is continuous and separates them in the right order.
        avg(secs_from_origin)                         as mean_secs_from_origin,
        cast(count(distinct journey_key) as bigint)   as n_obs
    from observations
    group by line, direction, day_type, atco_code

),

sequenced as (

    select
        s.line,
        s.direction,
        s.day_type,
        s.atco_code,
        s.median_secs_from_origin,
        s.n_obs,
        -- The sequence is a ranking, not a measurement: seq 1 is the stop the
        -- median journey reaches first. Ties broken on atco_code so a rebuild
        -- never silently renumbers the route.
        cast(row_number() over (
            partition by s.line, s.direction, s.day_type
            order by s.median_secs_from_origin, s.mean_secs_from_origin, s.atco_code
        ) as integer) as seq
    from by_grain s
    -- Fewer than five journeys is not a stop on the route, it is a diversion,
    -- a layover in a side street, or a stop on the *other* side of the road
    -- that a couple of journeys clipped.
    -- Ceiling: 5 journeys across the whole extract window.
    where s.n_obs >= 5

)

select
    q.line,
    q.direction,
    q.day_type,
    q.atco_code,
    i.stop_name,
    i.locality,
    i.lat,
    i.lon,
    q.seq,
    q.median_secs_from_origin,
    q.n_obs
from sequenced q
join stop_identity i using (atco_code)
