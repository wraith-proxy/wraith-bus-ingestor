{{ config(materialized='table') }}

{#
  The training set for the ETA regression.

  One row per ping of every surviving journey, answering the only question the
  API actually asks: "given that this bus still has X km of route in front of
  it, how many seconds will that take?" Fitting remaining_secs on remaining_km
  over millions of these rows is what eta_models does.

  Note this is measured along the *observed path*, not as the crow flies. The
  path is itself a chord approximation -- pings are 3 minutes apart, so the
  straight hops cut every corner -- which biases path length down by something
  like 10-20% on winding urban routes, and therefore biases the fitted speed
  down by the same amount. It is consistent, which is what matters for a
  regression, but it means the slope should be read as "seconds per observed
  km", not as a claim about the odometer.
#}

with journeys as (

    -- int_journeys_shaped_and_directed has already thrown out the implausible journeys, so
    -- this join is also the filter.
    select
        journey_key,
        direction,
        operator,
        end_ts,
        path_km,
        start_cum_km,
        start_ping_seq,
        end_ping_seq
    from {{ ref('int_journeys_shaped_and_directed') }}

),

pings as (

    select
        journey_key,
        line,
        hour_band,
        day_type,
        ping_ts,
        ping_seq,
        cum_km
    from {{ ref('int_pings_segmented_to_journeys') }}

),

joined as (

    select
        p.journey_key,
        p.line,
        j.direction,
        j.operator,
        -- The band of THIS ping, not of the journey. The API looks the model up
        -- by the band of the current clock time, so the training rows have to be
        -- bucketed the same way; a journey that starts at 09:50 contributes its
        -- first pings to am_peak and the rest to inter, which is correct.
        p.hour_band,
        -- Likewise the day of THIS ping, derived from local time. It matters at
        -- the seam: a journey that leaves at 23:50 on Saturday is still running
        -- at 00:10 on Sunday, and the API asking "what is the pace right now"
        -- at 00:10 is asking a Sunday question.
        p.day_type,
        p.ping_ts,
        p.ping_seq,
        j.start_ping_seq,
        j.end_ping_seq,
        p.cum_km,
        -- Distance still to run, measured inside the trimmed journey: total
        -- trimmed path, minus how far along it this ping already is.
        j.path_km - (p.cum_km - j.start_cum_km)       as remaining_km,
        date_diff('second', p.ping_ts, j.end_ts)      as remaining_secs
    from pings p
    join journeys j using (journey_key)

),

final as (

    select
        journey_key,
        line,
        direction,
        operator,
        day_type,
        hour_band,
        ping_ts,
        ping_seq,
        remaining_km,
        remaining_secs
    from joined
    -- The last two pings of a journey sit at remaining ~= 0 by construction.
    -- They carry no information about pace, they are the most numerous single
    -- value of remaining_km, and left in they drag the fitted intercept toward
    -- zero and flatten r2 -- so drop them. The head is trimmed too: pings
    -- before the journey started moving are not part of the run.
    where ping_seq >= start_ping_seq
      and ping_seq <= end_ping_seq - 2
      -- Floating-point cumulative sums can leave a hair of negative remainder
      -- on the final hops; a non-positive distance is not a training example.
      and remaining_km > 0
      and remaining_secs > 0

)

select * from final
