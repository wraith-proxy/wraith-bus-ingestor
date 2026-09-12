{#
  Observed lateness per (line, direction, day_type, hour_band), for the RAG
  colouring on the map.

  delay = when the bus actually finished - when it was scheduled to arrive,
  in seconds, positive meaning late. CONTRACT.md s3 keeps the late/early/on_time
  thresholds in backend/app.py, not here: this mart reports the distribution,
  the API decides what counts as late.

  EXPECT THIS TABLE TO BE EMPTY OR NEARLY EMPTY. dest_aimed_arr only started
  arriving in the feed on 2026-09-12, and the extract window is the days before
  it. Worse, the moment journey_ref switches from NULL to a value it triggers a
  journey break, so the few pings that do carry a schedule sit in
  freshly-started journeys too short to survive the plausibility filters. Both
  effects disappear once a full post-patch day has been extracted. The model is
  written so that it builds and returns zero rows without erroring. If it is
  still empty after a week of post-patch data, the ingestor is not writing
  dest_aimed_arr, and the problem is not here.

  Caveat worth knowing when the numbers do appear: the "actual arrival" here is
  the last moving ping of the journey, which is where the bus stopped, not
  necessarily the scheduled destination stop. It is a good proxy for a journey
  that ran to completion and a poor one for a journey that was curtailed.
#}

with journeys as (

    select
        line,
        direction,
        day_type,
        hour_band,
        end_ts,
        dest_aimed_arr_ts
    from {{ ref('int_journey_shape') }}
    where dest_aimed_arr_ts is not null

),

delays as (

    select
        line,
        direction,
        day_type,
        hour_band,
        date_diff('second', dest_aimed_arr_ts, end_ts) as delay_s
    from journeys
    -- Anything beyond plus or minus two hours is a mis-joined schedule, not a
    -- late bus; a two-hour-late bus has been cancelled and replaced.
    where abs(date_diff('second', dest_aimed_arr_ts, end_ts)) <= 7200

)

select
    line,
    direction,
    day_type,
    hour_band,
    cast(median(delay_s) as double)                as median_delay_s,
    cast(quantile_cont(delay_s, 0.9) as double)    as p90_delay_s,
    cast(count(*) as bigint)                       as n_journeys
from delays
group by line, direction, day_type, hour_band
-- One journey's delay is an anecdote. Three is a weak signal, which is all this
-- will have for a while; the API should treat a small n_journeys accordingly.
having count(*) >= 3
