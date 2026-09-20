{#
  No (journey_key, ping_seq) may appear twice in int_pings_segmented_to_journeys.

  Why this exists: the model's own header records an incident where two
  different physical journeys were numbered identically and produced a duplicate
  ping_seq downstream in int_pings_matched_to_stops. journey_key is only stable
  if a journey's start ping lies inside the window an incremental run reads, so
  a read window that is too narrow (or a change to how journeys are keyed) would
  re-key journeys and show up here first.

  Restricted to the last 3 dates so it stays cheap: those are the only rows an
  incremental run can have touched.
#}

select
    journey_key,
    ping_seq,
    count(*) as n
from {{ ref('int_pings_segmented_to_journeys') }}
where ping_local_date >= (select max(ping_local_date) from {{ ref('int_pings_segmented_to_journeys') }}) - interval '2 days'
group by journey_key, ping_seq
having count(*) > 1
