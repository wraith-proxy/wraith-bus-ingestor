{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='ping_local_date',
    post_hook="delete from {{ this }} where ping_local_date < (select max(ping_local_date) from {{ this }}) - interval '{{ retention_days() }} days'"
) }}

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

  ---------------------------------------------------------------------------
  Incremental strategy
  ---------------------------------------------------------------------------
  This is the single most expensive model in the project (724s measured over
  the old 22-day, ~45M-row window; retention_days() is now 8) and the one
  three downstream models scan, so it is incremental by ping_local_date
  rather than a full `table` rebuild every run.

  Read window vs. write window: `lag() over (partition by bus_id order by
  ping_ts)` needs real history at the edge of whatever window gets
  reprocessed, or a bus mid-journey when local midnight ticks over looks like
  it has no previous ping and gets wrongly flagged as a journey start. So the
  `pings` CTE reads 3 days of history on an incremental run (2 days to
  rewrite + 1 day of buffer purely for lag() context), every window function
  below runs over that full 3-day read, and only the final `filtered` step
  keeps the 2 days that actually get (re)written. The buffer size is fixed at
  1 day, not a tuning knob -- it comes from the 20-minute gap / 9-minute
  standing break conditions above, which need nowhere near a full day of
  context to resolve correctly.

  `delete+insert` with `unique_key='ping_local_date'` is dbt's standard
  "partition replace" pattern even though the key repeats across many rows:
  it deletes every existing row whose ping_local_date appears anywhere in the
  new batch, then inserts the whole batch -- exactly "replace the last 2
  days" semantics, with no need for a genuinely unique per-row key.

  The post-hook prunes anything older than the retention window so this table
  doesn't grow past what extract.py actually still has behind it.
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
        ping_local_date,
        hour_band,
        day_type,
        lat,
        lon,
        origin_aimed_dep_ts,
        dest_aimed_arr_ts
    from {{ ref('stg_bods__vehicle_pings') }}
    {% if is_incremental() %}
    where ping_local_date >= (select max(ping_local_date) from {{ this }}) - interval '3 days'
    {% endif %}

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
        -- See int_journeys_shaped_and_directed.
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
        -- A running "sum of is_journey_start" would give every journey a
        -- distinct increasing number, but that number is only stable WITHIN
        -- one read: on an incremental run it restarts near zero for whatever
        -- vehicle has journeys in the (narrow) window being reprocessed,
        -- while a non-reprocessed older region of this same table still
        -- carries numbers from a wide, full-history read. A busy vehicle can
        -- rack up dozens of journey-starts in just the incremental window,
        -- so the two numbering schemes can coincidentally collide on the
        -- same suffix for two totally different physical journeys -- caught
        -- live as a duplicate ping_seq in int_pings_matched_to_stops the
        -- first time this ran as a true incremental (non-first) build.
        --
        -- journey_key has to be a property of the journey itself, not of how
        -- many journeys came before it in whatever slice happened to be
        -- read. Carrying the current journey's own start ping_ts FORWARD
        -- gives exactly that: the same physical journey always starts at the
        -- same instant no matter how wide a window it is read through.
        --
        -- max(case ...) rather than last_value(... ignore nulls): both are
        -- correct here (ping_ts is monotonic within the frame, so the max
        -- non-null value up to the current row IS the most recent one), but
        -- IGNORE NULLS window frames OOM'd the box outright at full 45M-row
        -- scale -- twice, on two different formulations -- while max() is
        -- the same accumulator shape as the sum() this replaces, which has
        -- run this exact scale repeatedly tonight without incident. NULLs
        -- need no special handling: max() already ignores them.
        bus_id || '#' || cast(
            epoch_ms(
                max(case when is_journey_start = 1 then ping_ts end) over (
                    partition by bus_id order by ping_ts
                    rows between unbounded preceding and current row
                )
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
        ping_local_date,
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
        -- int_pings_measured_to_journey_end subtracts this from the journey
        -- total to get "how far is left"; int_journeys_shaped_and_directed
        -- takes its max as the path length.
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
        -- and last genuinely moving ping lets int_journeys_shaped_and_directed
        -- trim both ends.
        -- Ceiling: moving = at least 50 m in a 3-minute bucket (~1 km/h).
        -- Measured effect of trimming: r2 0.52 -> 0.69, intercept 1060s -> 749s.
        min(ping_seq) filter (where hop_km >= 0.05)
            over (partition by journey_key) as first_moving_seq,
        max(ping_seq) filter (where hop_km >= 0.05)
            over (partition by journey_key) as last_moving_seq
    from measured

),

filtered as (

    select *
    from final
    {% if is_incremental() %}
    -- Only the last 2 days are ever (re)written; the 1-day buffer read above
    -- was for lag() context only and never lands here.
    where ping_local_date >= (select max(ping_local_date) from {{ this }}) - interval '2 days'
    {% endif %}

)

select * from filtered
