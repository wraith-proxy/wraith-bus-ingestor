{#
  The observed stop sequence for each (operator, line, direction, day_type) --
  what the API answers "what was the last stop" and "what is the next stop"
  from.

  operator joined the grain on 2026-09-12, the day it first became available
  (see the KNOWN LIMITATION this docstring used to carry, and CONTRACT.md /
  bus-marts.odcs.yaml). A line number is not a route: '88' is a Sheffield
  cross-city route AND a Wilmslow-Hale route AND something in rural
  Lancashire, and grouping on line alone merged all of them into one
  zig-zagging sequence. operator is emitted both ways -- real value AND
  pooled as '*' -- exactly like day_type already was, so the API's fallback
  chain can prefer the operator-specific sequence and still resolve for a
  line it has never seen an operator for (all pre-2026-09-12 data, which is
  most of the training window for a while yet).

  There is no timetable in this project, so the sequence is reconstructed from
  behaviour. For every journey that passed within 120 m of a stop we know how
  many seconds into the journey that happened; the median of that over all
  journeys is a stable "this stop is about 14 minutes along the route", and
  ordering the stops by that median gives the route order.

  Median rather than mean, because one journey stuck in a road closure would
  otherwise reorder the route and produce confidently wrong next-stop answers.
  As of 2026-09-14 this is computed as a bounded t-digest approximation
  (approx_quantile), not DuckDB's exact median() -- see
  int_line_stops_real_real.sql (and its three sibling grain models) for why,
  and how the "true median over every journey in the pool" property is
  preserved anyway. The four grains used to be CTEs inline in this file; they
  are now their own dbt models under models/intermediate/, split out to fix a
  repeated full-host OOM -- see docs/ROADMAP.md's 1.1.1 entry for the incident
  history.

  Two row sets are emitted: one per real day_type, and one at day_type = '*'
  fitted across all days, so a lookup always resolves even for a line that only
  runs Monday to Friday. Like eta_models' fallbacks, the '*' set is
  rebuilt from the underlying observations rather than merged from the others.

  This mart matters more than it used to. The API refuses to estimate circular
  routes geometrically (origin and destination are the same stop, so the
  geometry says "you have arrived" for the whole loop), and falls back to this
  stop sequence instead -- so for those ~340 vehicles this table is the only
  thing that can produce an ETA at all. int_journeys_shaped_and_directed has a
  dedicated direction rule for circular routes for that reason.

  HOW GOOD IS THE RESULT? Measured, not assumed: walk the published stops in
  seq order, sum the distance, divide by the route's actual length. A correct
  sequence scores ~1. This one scores 6-9x on a geographically coherent line
  and 200-480x on a line whose number is reused in several towns.
  (Baseline predates the approx_quantile switch and the by-grain-model split;
  re-measure after either change and expect no material regression; see
  migration notes.)

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

    select * from {{ ref('int_pings_matched_to_stops') }}

),

by_grain as (

    -- A union of small, already-aggregated rows (one per grain), not raw
    -- observations. Each grain is its own dbt model under
    -- models/intermediate/ (int_line_stops_real_real, _real_star,
    -- _star_real, _star_star), each independently materialized as a table
    -- and run at --threads 1 (see build.sh) so only one grain's
    -- hash-aggregate state is ever open in memory at a time -- see
    -- int_line_stops_real_real.sql for the full incident history and why
    -- this used to be four CTEs unioned inline here instead.
    select * from {{ ref('int_line_stops_real_real') }}
    union all
    select * from {{ ref('int_line_stops_real_star') }}
    union all
    select * from {{ ref('int_line_stops_star_real') }}
    union all
    select * from {{ ref('int_line_stops_star_star') }}

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

sequenced as (

    select
        s.operator,
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
            partition by s.operator, s.line, s.direction, s.day_type
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
    q.operator,
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
