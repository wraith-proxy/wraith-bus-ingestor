{#
  Observed lateness per (line, direction, day_type, hour_band), for the RAG
  colouring on the map.

  delay = when the bus actually finished - when it was scheduled to arrive,
  in seconds, positive meaning late. CONTRACT.md s3 keeps the late/early/on_time
  thresholds in backend/app.py, not here: this mart reports the distribution,
  the API decides what counts as late.

  The per-journey delays, and the filters on which journeys count, live in
  int_journey_delays. It keeps history_days() local dates of them however
  short extract.py's window of raw pings is; this model is only the aggregate
  over all of them.

  Caveat worth knowing when the numbers do appear: the "actual arrival" here is
  the last moving ping of the journey, which is where the bus stopped, not
  necessarily the scheduled destination stop. It is a good proxy for a journey
  that ran to completion and a poor one for a journey that was curtailed.
#}

select
    line,
    direction,
    day_type,
    hour_band,
    cast(median(delay_s) as double)                as median_delay_s,
    cast(quantile_cont(delay_s, 0.9) as double)    as p90_delay_s,
    cast(count(*) as bigint)                       as n_journeys
from {{ ref('int_journey_delays') }}
group by line, direction, day_type, hour_band
-- One journey's delay is an anecdote. Three is a weak signal, which is all this
-- will have for a while; the API should treat a small n_journeys accordingly.
having count(*) >= 3
