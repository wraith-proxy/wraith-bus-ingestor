{{ config(materialized='table') }}

{#
  One row per journey: where it ran, how far, how long, which day pattern it
  belongs to, and which way round the route. Implausible journeys are dropped
  here, so everything downstream can take this table at face value.

  ---------------------------------------------------------------------------
  Direction, and why there are two ways of deciding it
  ---------------------------------------------------------------------------
  The feed's DirectionRef is NULL for essentially the whole training set, and a
  stop sequence without a direction is useless -- the outbound and inbound
  orders interleave into one meaningless list. So direction is inferred, by one
  of two methods depending on the shape of the route.

  LINEAR ROUTES (A to B and back). Take each journey's compass bearing from
  first ping to last, find the line's modal 45-degree bearing bin, and split
  journeys on whether they lie within 90 degrees of it. Works well: the two
  directions of a linear route point roughly opposite ways.

  CIRCULAR ROUTES (Liverpool ONE, Queen Square, airport circulars). The same
  method is worse than useless here. A loop ends within a few hundred metres of
  where it started, so its first-to-last bearing is GPS noise, and journeys
  running the identical loop get scattered at random between 'inbound' and
  'outbound' -- producing two half-populated, incoherent stop sequences instead
  of one good one. Since the API now falls back to the stop sequence for
  exactly these routes (it refuses to estimate them geometrically, because
  origin and destination are the same stop), that scattering is the thing
  standing between ~340 circular-route vehicles and a usable ETA.

  For a loop, the meaningful question is not "which way is it pointing" but
  "which way round is it going". That is the SIGN OF THE ENCLOSED AREA, via the
  shoelace formula summed in int_bus_journeys: positive is anticlockwise,
  negative is clockwise. It is exact, it needs no reference bearing, and two
  buses running the same loop the same way always agree.

  A line is treated as circular when the median of straight_km / path_km across
  its journeys is below 0.35 -- i.e. the typical journey ends up much nearer its
  start than the distance it covered. Ceiling: 0.35. Per line rather than per
  journey, so that one short-turn working on a loop route does not get bucketed
  by a different rule than the rest of its line.

  Note the circulation sense is mapped onto the SAME 'outbound'/'inbound'
  tokens the rest of the system uses (anticlockwise -> 'outbound'), rather than
  introducing 'clockwise'/'anticlockwise' values. The names are arbitrary for a
  loop, and inventing new tokens would break every direction lookup in the API
  for no gain. What matters is that the bucketing is CONSISTENT, not what it is
  called.

  In all cases a real DirectionRef from the feed wins, so both heuristics
  retire themselves as post-2026-09-12 data accumulates.
#}

with pings as (

    -- Trim the stationary head and tail off every journey. int_bus_journeys
    -- marks the first and last ping at which the vehicle was actually moving;
    -- everything outside that window is the bus standing on the stand, which
    -- is real but is not travel and must not be measured as if it were.
    select *
    from {{ ref('int_bus_journeys') }}
    where first_moving_seq is not null
      and ping_seq between first_moving_seq and last_moving_seq

),

per_journey as (

    select
        journey_key,
        any_value(bus_id)                       as bus_id,
        any_value(line)                         as line,
        -- max() over a mostly-NULL column is a cheap "did the feed ever tell us
        -- the direction for this journey".
        max(feed_direction)                     as feed_direction,
        max(operator)                           as operator,
        max(journey_ref)                        as journey_ref,
        max(origin_ref)                         as origin_ref,
        max(dest_ref)                           as dest_ref,
        max(origin_aimed_dep_ts)                as origin_aimed_dep_ts,
        max(dest_aimed_arr_ts)                  as dest_aimed_arr_ts,
        count(*)                                as n_pings,
        min(ping_ts)                            as start_ts,
        max(ping_ts)                            as end_ts,
        -- arg_min/arg_max pick the value of one column at the row where another
        -- is minimal: "the latitude at the earliest ping", in one pass.
        arg_min(lat, ping_ts)                   as start_lat,
        arg_min(lon, ping_ts)                   as start_lon,
        arg_max(lat, ping_ts)                   as end_lat,
        arg_max(lon, ping_ts)                   as end_lon,
        -- cum_km is measured from the UNtrimmed start, so the trimmed path
        -- length is the span of cum_km across the window we kept.
        max(cum_km) - min(cum_km)               as path_km,
        min(cum_km)                             as start_cum_km,
        min(ping_seq)                           as start_ping_seq,
        max(ping_seq)                           as end_ping_seq,
        sum(shoelace_term)                      as shoelace_sum,
        -- The band and the day the journey STARTED in. A journey is short
        -- enough that using one value per journey keeps it inside one grain of
        -- the journey-level aggregates.
        arg_min(hour_band, ping_ts)             as hour_band,
        arg_min(day_type, ping_ts)              as day_type
    from pings
    group by journey_key

),

shaped as (

    select
        *,
        {{ haversine_km('start_lat', 'start_lon', 'end_lat', 'end_lon') }} as straight_km,
        date_diff('second', start_ts, end_ts)                             as duration_secs,
        {{ bearing_deg('start_lat', 'start_lon', 'end_lat', 'end_lon') }}  as journey_bearing,
        -- Close the polygon: the shoelace sum covers ping 1..n, this adds the
        -- edge from ping n back to ping 1. Twice the signed enclosed area.
        shoelace_sum + ((end_lon * start_lat) - (start_lon * end_lat))    as signed_area_2
    from per_journey

),

plausible as (

    select
        *,
        path_km / (duration_secs / 3600.0) as mean_speed_kmh
    from shaped
    -- Every filter below exists to keep a non-journey out of the regression.
    where n_pings >= 5                          -- fewer than ~15 min of observations: not enough shape to fit anything
      and path_km >= 0.5                        -- under 500 m the vehicle never left the stand; all "distance remaining" is GPS jitter
      and duration_secs >= 240                  -- under 4 min is a fragment left over from a break, not a service run
      and duration_secs <= 5 * 3600             -- over 5 h means the segmentation failed to find a layover; keep it out rather than trust it
      and path_km / (duration_secs / 3600.0) <= 90   -- faster than 90 km/h average is a vehicle-ref collision we failed to split
      and path_km / (duration_secs / 3600.0) >= 1    -- slower than 1 km/h is a parked bus with drifting GPS

),

-- ---------------------------------------------------------------------------
-- Journeys whose ending we actually witnessed
-- ---------------------------------------------------------------------------
--
-- The extract window is NOT a contiguous range. The feed was out from
-- 2026-08-28 to 08-30, and the most recent file is today's, which stops
-- wherever the extract ran. A journey that was still running when the data
-- stops does not end -- it is CUT, and its last observed ping is not its
-- destination.
--
-- Left in, those journeys are actively harmful rather than merely missing:
-- every one of them teaches the regression that a bus which still had half a
-- route ahead of it arrived in zero seconds. They are also the fastest-looking
-- journeys in the dataset, so they bias the slope low precisely where it hurts.
--
-- Rather than hardcode the known gap dates -- which would go stale the next
-- time the feed drops out -- find the coverage edges from the data itself: the
-- 10-minute buckets after which nothing is observed for at least 20 minutes.
-- On this window that is three timestamps (end of 08-27, end of 08-31, and the
-- end of today's file), and it costs one scan of a few thousand buckets.

coverage_buckets as (

    select distinct time_bucket(interval '10 minutes', ping_ts) as bucket
    from {{ ref('int_bus_journeys') }}

),

coverage_ends as (

    -- A bucket with no successor within 20 minutes is the edge of a data block.
    -- 20 minutes matches the journey-break threshold, so "the feed stopped" and
    -- "the bus stopped reporting" are judged on the same clock.
    select bucket as ends_at
    from (
        select
            bucket,
            lead(bucket) over (order by bucket) as next_bucket
        from coverage_buckets
    )
    where next_bucket is null
       or next_bucket > bucket + interval '20 minutes'

),

observed_to_completion as (

    select p.*
    from plausible p
    where not exists (
        select 1
        from coverage_ends e
        where p.end_ts >  e.ends_at - interval '20 minutes'
          and p.end_ts <= e.ends_at + interval '10 minutes'
    )

),

-- ---------------------------------------------------------------------------
-- Direction inference
-- ---------------------------------------------------------------------------

line_shape as (

    -- How loop-like is this line? straight_km / path_km near 1 is an A-to-B
    -- run; near 0 is a circuit that comes back to where it started.
    --
    -- Grained on operator too, not just line: '88' is a genuinely different
    -- route per operator, and pooling them the way this used to would (and,
    -- for historical data, still does) treat a Sheffield cross-city route and
    -- a Wilmslow-Hale route as one shape. operator is coalesced to a sentinel
    -- rather than left NULL because SQL joins never match NULL to NULL --
    -- see int_bus_journeys' prev_operator for the same trick. Every row
    -- before 2026-09-12 has operator_key = '~none~' for everything, so this
    -- is a no-op on historical data: it only starts disambiguating once the
    -- feed actually supplies an operator.
    select
        coalesce(operator, '~none~') as operator_key,
        line,
        median(straight_km / path_km) as median_linearity,
        count(*)                      as n_journeys
    from observed_to_completion
    group by operator_key, line

),

line_is_circular as (

    select
        operator_key,
        line,
        median_linearity,
        median_linearity < 0.35 as is_circular_line
    from line_shape

),

line_bearing_bins as (

    -- 45-degree bins. Coarse on purpose: we only need to know which of two
    -- opposed halves of the compass a journey belongs to, and coarse bins are
    -- far more stable against a few wandering journeys than a mean would be.
    select
        coalesce(operator, '~none~')                 as operator_key,
        line,
        cast(floor(journey_bearing / 45) as integer) as bearing_bin,
        count(*)                                     as n_journeys,
        -- Circular mean of the bearings in the bin. Averaging 350 and 10
        -- arithmetically gives 180 (due south); via sin/cos it gives 0 (north).
        (degrees(atan2(avg(sin(radians(journey_bearing))),
                       avg(cos(radians(journey_bearing))))) + 360) % 360 as bin_mean_bearing
    from observed_to_completion
    group by operator_key, line, bearing_bin

),

line_reference_bearing as (

    select
        operator_key,
        line,
        bin_mean_bearing as reference_bearing
    from line_bearing_bins
    -- Tie-break on bearing_bin so the choice is deterministic across rebuilds;
    -- otherwise two equally-busy bins could swap 'outbound' and 'inbound'
    -- between runs and the API's stop sequences would flip.
    qualify row_number() over (partition by operator_key, line order by n_journeys desc, bearing_bin) = 1

),

directed as (

    select
        p.*,
        r.reference_bearing,
        coalesce(c.is_circular_line, false) as is_circular_line,
        c.median_linearity                  as line_median_linearity,
        case
            -- Circular route: which way ROUND, from the sign of the enclosed area.
            when coalesce(c.is_circular_line, false) then
                case when p.signed_area_2 >= 0 then 'outbound' else 'inbound' end
            -- Linear route: which way ALONG, from the bearing against the line's axis.
            when r.reference_bearing is null then 'outbound'   -- defensive; cannot happen while the line has journeys
            when {{ bearing_delta('p.journey_bearing', 'r.reference_bearing') }} < 90 then 'outbound'
            else 'inbound'
        end as direction_inferred
    -- Explicit null-safe join rather than `using`: p.operator is genuinely
    -- NULL for all pre-2026-09-12 journeys, and `using`/`=` never matches
    -- NULL to NULL, which would silently drop the reference bearing for
    -- every historical journey.
    from observed_to_completion p
    left join line_reference_bearing r
        on r.operator_key = coalesce(p.operator, '~none~') and r.line = p.line
    left join line_is_circular c
        on c.operator_key = coalesce(p.operator, '~none~') and c.line = p.line

),

final as (

    select
        journey_key,
        bus_id,
        line,
        feed_direction,
        -- The feed always wins when it has an opinion; the heuristics only fill
        -- the gap. Never '*' -- '*' means "the fallback grain of the model",
        -- not "a bus travelling in an unknown direction".
        coalesce(feed_direction, direction_inferred) as direction,
        direction_inferred,
        is_circular_line,
        line_median_linearity,
        signed_area_2,
        operator,
        journey_ref,
        origin_ref,
        dest_ref,
        origin_aimed_dep_ts,
        dest_aimed_arr_ts,
        hour_band,
        day_type,
        n_pings,
        start_cum_km,
        start_ping_seq,
        end_ping_seq,
        start_ts,
        end_ts,
        start_lat,
        start_lon,
        end_lat,
        end_lon,
        straight_km,
        path_km,
        duration_secs,
        mean_speed_kmh,
        journey_bearing,
        reference_bearing
    from directed

)

select * from final
