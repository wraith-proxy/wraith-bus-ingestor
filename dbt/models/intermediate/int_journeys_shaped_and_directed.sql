{{ config(materialized='table') }}

{#
  One row per journey: where it ran, how far, how long, which day pattern it
  belongs to, and which way round the route. Implausible journeys are dropped
  here, so everything downstream can take this table at face value.

  ---------------------------------------------------------------------------
  Direction: the heuristic is retired (ROADMAP.md 1.2.0, 2026-09-23)
  ---------------------------------------------------------------------------
  This used to also INFER a direction from journey geometry (first-to-last
  compass bearing for linear routes, shoelace-formula circulation sense for
  loops) whenever the feed's own DirectionRef was NULL -- which, at the time
  that heuristic was written, was true for essentially every row. Measured
  against the build warehouse on 2026-09-23, once real DirectionRef data had
  accumulated: feed_direction is now present on 68.1% of journeys (of
  2,316,604 in the current retention window), and where BOTH values exist
  they disagree 53.9% of the time -- indistinguishable from chance. The
  heuristic's own inbound/outbound split is arbitrary per line (whichever
  bearing bin happens to have more journeys), so it was never trying to match
  the feed's convention in the first place; near-50% disagreement is exactly
  what that predicts, not evidence the feed is wrong. The feed also already
  reports 'clockwise'/'anticlockwise' directly for loop routes (18,831 rows),
  which the loop heuristic was reinventing less reliably.

  `direction` is now feed_direction, unmodified. It is NULL for the 31.9% of
  journeys the feed did not tag -- accepted, not patched over. Nothing
  downstream requires it non-null: eta_models and line_punctuality both group
  by direction among other columns, so a NULL forms its own grain like any
  other value, and pick_model() in backend/app.py already falls through to a
  coarser grain on a miss (it was already designed to tolerate the live
  feed's direction not matching a training-set label -- see that function's
  own docstring). A live bus with no DirectionRef was never run through a
  heuristic either: backend/app.py reads direction straight off the ping.
#}

with pings as (

    -- Trim the stationary head and tail off every journey. int_pings_segmented_to_journeys
    -- marks the first and last ping at which the vehicle was actually moving;
    -- everything outside that window is the bus standing on the stand, which
    -- is real but is not travel and must not be measured as if it were.
    select *
    from {{ ref('int_pings_segmented_to_journeys') }}
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
        date_diff('second', start_ts, end_ts)                             as duration_secs
    from per_journey

),

plausible as (

    select
        *,
        {{ avg_speed_kmh('path_km', 'duration_secs') }} as mean_speed_kmh
    from shaped
    -- Every filter below exists to keep a non-journey out of the regression.
    where n_pings >= 5                          -- fewer than ~15 min of observations: not enough shape to fit anything
      and path_km >= 0.5                        -- under 500 m the vehicle never left the stand; all "distance remaining" is GPS jitter
      and duration_secs >= 240                  -- under 4 min is a fragment left over from a break, not a service run
      and duration_secs <= 5 * 3600             -- over 5 h means the segmentation failed to find a layover; keep it out rather than trust it
      and {{ avg_speed_kmh('path_km', 'duration_secs') }} <= 90   -- faster than 90 km/h average is a vehicle-ref collision we failed to split
      and {{ avg_speed_kmh('path_km', 'duration_secs') }} >= 1    -- slower than 1 km/h is a parked bus with drifting GPS

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
    from {{ ref('int_pings_segmented_to_journeys') }}

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

final as (

    select
        journey_key,
        bus_id,
        line,
        feed_direction,
        -- The feed is now the only source -- see the header. NULL when the
        -- feed didn't tag this journey; nothing downstream requires non-null.
        feed_direction as direction,
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
        mean_speed_kmh
    from observed_to_completion

)

select * from final
