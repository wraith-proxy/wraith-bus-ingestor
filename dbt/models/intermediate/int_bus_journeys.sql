{{ config(materialized='table') }}

{#
  Cut each vehicle's continuous ping stream into journeys.

  A bus_id is reused all day: the same vehicle runs the 88 outbound, sits at the
  terminus, runs it back, then goes on the 130. Fitting an ETA model over the
  raw stream would measure "distance remaining until this vehicle stops
  reporting tonight", which is nonsense. So we segment.

  Break conditions, ORed:
    1. journey_ref changes  -- authoritative when present (post 2026-09-12 only)
    2. gap > 20 minutes     -- pings are 3-minute buckets, so 20 min is ~7
                               consecutive missing buckets: depot, end of shift,
                               or sustained signal loss.
    3. line changes         -- the vehicle has been reassigned to another service
    4. operator changes     -- only ever non-NULL post 2026-09-12; see 5.
    5. implied speed > 120 km/h between consecutive pings
    6. the vehicle has just pulled away after standing still for ~9 minutes

  Why 5 exists. Operators mint their own vehicle refs, so bus_id '3104' is
  fifteen different physical buses in fifteen different towns, and without
  `operator` (NULL in all historical data) nothing else tells them apart. Their
  pings interleave in time, and on the same line number they would otherwise be
  glued into one "journey" that teleports 300 km between pings.
  Ceiling: 120 km/h. Raise it if long-distance coaches start being dropped.

  Why 6 exists, and it is the important one. Buses keep transmitting while they
  sit on the stand at a terminus, so condition 2 never fires on a layover: a bus
  working the same route all day came out as ONE journey of a dozen round trips.
  Measured on this dataset, 60% of surviving journeys ended within 30% of a
  straight line of where they started -- i.e. they were out-and-backs glued
  together, which makes "distance remaining" meaningless and makes the
  first-ping-to-last-ping bearing (used to infer direction) pure noise.
  Detecting the layover directly fixes it: with condition 6 the loop-back share
  drops to 16% and the median journey becomes a plausible 45 min / 9.8 km.
  Ceilings: stood still = moved < 500 m over the previous 9 minutes (3 ping
  buckets). A bus crawling below 3.3 km/h for nine solid minutes in extreme
  congestion will be split in two; that loses a little data but corrupts none.

  The trick in `keyed`: flag each break as 1, then a running sum() of that flag
  over the vehicle's pings gives every journey a distinct increasing number.
  This is the standard "gaps and islands" pattern and it is worth knowing --
  it turns a row-by-row loop into one window function.

  Materialised as a table: three downstream models scan it, and recomputing
  seven window functions over 20M rows three times is the most expensive thing
  this project could do.
#}

with pings as (

    select
        bus_id,
        line,
        feed_direction,
        operator,
        journey_ref,
        origin_ref,
        dest_ref,
        ping_ts,
        hour_band,
        day_type,
        lat,
        lon,
        origin_aimed_dep_ts,
        dest_aimed_arr_ts
    from {{ ref('stg_bus_pings') }}

),

with_previous as (

    select
        *,
        lag(ping_ts) over w as prev_ping_ts,
        lag(line)    over w as prev_line,
        lag(lat)     over w as prev_lat,
        lag(lon)     over w as prev_lon,
        -- Three buckets back, for the "standing on the stand" test below.
        lag(lat, 3)     over w as lat_3_back,
        lag(lon, 3)     over w as lon_3_back,
        lag(ping_ts, 3) over w as ping_ts_3_back,
        -- journey_ref is NULL for every historical row; coalescing to a sentinel
        -- keeps "NULL then NULL" from reading as a change.
        lag(coalesce(journey_ref, '~none~')) over w as prev_journey_ref,
        lag(coalesce(operator,    '~none~')) over w as prev_operator
    from pings
    window w as (partition by bus_id order by ping_ts)

),

with_step as (

    select
        *,
        case when prev_ping_ts is null then null
             else {{ haversine_km('prev_lat', 'prev_lon', 'lat', 'lon') }}
        end as step_km,
        -- One term of the shoelace (surveyor's) formula, treating lon as x and
        -- lat as y. Summed over a journey it gives twice the signed area the
        -- path encloses, and the SIGN of that is the direction of circulation:
        -- positive anticlockwise, negative clockwise.
        -- This is the only usable direction signal for a circular route, whose
        -- first-to-last bearing is noise because it ends where it started.
        -- See int_journey_shape.
        case when prev_ping_ts is null then 0.0
             else (prev_lon * lat) - (lon * prev_lat)
        end as shoelace_term,
        date_diff('second', prev_ping_ts, ping_ts) as step_secs,
        -- "Standing still": less than 500 m of ground covered over the previous
        -- three ping buckets, and those buckets really were consecutive (<= 15
        -- min, so a reporting gap is not mistaken for a stationary bus).
        (
            date_diff('second', ping_ts_3_back, ping_ts) between 1 and 900
            and {{ haversine_km('lat_3_back', 'lon_3_back', 'lat', 'lon') }} < 0.5
        ) as is_standing
    from with_previous

),

with_standing_history as (

    select
        *,
        lag(is_standing) over (partition by bus_id order by ping_ts) as was_standing
    from with_step

),

flagged as (

    select
        *,
        case
            when prev_ping_ts is null                                              then 1
            when step_secs > 20 * 60                                               then 1
            when line is distinct from prev_line                                   then 1
            when coalesce(journey_ref, '~none~') is distinct from prev_journey_ref  then 1
            when coalesce(operator, '~none~')    is distinct from prev_operator     then 1
            when step_secs > 0 and (step_km / (step_secs / 3600.0)) > 120          then 1
            -- Layover finished: it was standing, now it is not. The new journey
            -- starts with the first moving ping, which is what we want.
            when was_standing and not is_standing                                  then 1
            else 0
        end as is_journey_start
    from with_standing_history

),

keyed as (

    select
        *,
        bus_id || '#' || cast(
            sum(is_journey_start) over (
                partition by bus_id order by ping_ts
                rows between unbounded preceding and current row
            ) as varchar
        ) as journey_key
    from flagged

),

measured as (

    select
        *,
        -- Distance travelled since the previous ping. Zero on the first ping of
        -- a journey, because there the lag() still points at the *previous*
        -- journey's last position (often a different town).
        case when is_journey_start = 1 then 0.0 else step_km end as hop_km,
        row_number() over (partition by journey_key order by ping_ts) as ping_seq
    from keyed

),

final as (

    select
        journey_key,
        bus_id,
        line,
        feed_direction,
        operator,
        journey_ref,
        origin_ref,
        dest_ref,
        ping_ts,
        hour_band,
        day_type,
        lat,
        lon,
        hop_km,
        case when is_journey_start = 1 then 0.0 else shoelace_term end as shoelace_term,
        origin_aimed_dep_ts,
        dest_aimed_arr_ts,
        ping_seq,
        -- Cumulative path length from the journey's first ping to this one.
        -- int_ping_remaining subtracts this from the journey total to get
        -- "how far is left"; int_journey_shape takes its max as the path length.
        sum(hop_km) over (
            partition by journey_key order by ping_ts
            rows between unbounded preceding and current row
        ) as cum_km,
        min(ping_ts) over (partition by journey_key) as journey_start_ts,
        max(ping_ts) over (partition by journey_key) as journey_end_ts,
        count(*)     over (partition by journey_key) as journey_n_pings,
        -- A bus that is sitting at its terminus still reports. Those pings are
        -- part of the journey's row range but not part of its *running*, and
        -- leaving them on the end means every journey finishes with several
        -- minutes of "0 km remaining, still 6 minutes to go" -- which the
        -- regression absorbs into a large positive intercept. Marking the first
        -- and last genuinely moving ping lets int_journey_shape trim both ends.
        -- Ceiling: moving = at least 50 m in a 3-minute bucket (~1 km/h).
        -- Measured effect of trimming: r2 0.52 -> 0.69, intercept 1060s -> 749s.
        min(ping_seq) filter (where hop_km >= 0.05)
            over (partition by journey_key) as first_moving_seq,
        max(ping_seq) filter (where hop_km >= 0.05)
            over (partition by journey_key) as last_moving_seq
    from measured

)

select * from final
