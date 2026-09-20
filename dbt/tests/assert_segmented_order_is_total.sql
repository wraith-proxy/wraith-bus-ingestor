{{ config(severity='warn') }}

{#
  No two rows of int_pings_segmented_to_journeys may share
  (operator, bus_id, ping_ts, line).

  Why this exists: the model's windows order by a total key
  (ping_ts, recorded_at, line, lat, lon) so that two runs over the same data
  produce the same journeys. That only holds if the extract really does hand
  it at most one row per (operator, bus_id, 3-minute bucket, line): the
  extract's SAMPLE BY grouping guarantees it by construction, so a duplicate
  here means the grouping regressed (operator or line dropped from
  dbt/extract.py's KEYS again) and the segmentation is silently
  non-deterministic. Before 2026-09-20 it was: 6.2% of a day's rows shared a
  (bus_id, timestamp) with another vehicle.

  severity is warn, not error: this guards a property of the INPUT, and a
  wrong "error" at 03:00 would block the publish of an otherwise good build.
  The Grafana "dbt tests failed or warned" rule pages on a warn, which is the
  visibility we want. Restricted to the last 2 dates so it stays cheap (an
  earlier count(distinct struct) version cost ~400 s and 6 GB on 5 days).
#}

select
    coalesce(operator, '~none~') as operator,
    bus_id,
    ping_ts,
    line,
    count(*) as n
from {{ ref('int_pings_segmented_to_journeys') }}
where ping_local_date >= (select max(ping_local_date) from {{ ref('int_pings_segmented_to_journeys') }}) - interval '1 day'
group by 1, 2, 3, 4
having count(*) > 1
