{{ config(materialized='table') }}

-- Promoted out of line_stops.sql (was the stop_identity CTE) so it builds
-- as its own real dbt model instead of running as one of two independent
-- subtrees under line_stops' final join. See docs/adr/0002.
--
-- any_value(), not mode(): mode() alone was measured OOM'ing this model in
-- isolation (2.19GB, no other branch competing) -- confirmed via
-- journalctl -k, no graceful DuckDB error, the same "per-group aggregate
-- state DuckDB's memory_limit doesn't reliably bound" limitation already
-- seen on approx_quantile. Checked before swapping, not assumed: 0 of
-- 173,639 stops (2026-09-15) have more than one distinct stop_name or
-- locality, so mode() and any_value() produce identical output on this
-- data -- any_value() just grabs a non-null value, no frequency-tracking.
-- Unlike the count(*) fix elsewhere in this project, this is an EMPIRICAL
-- fact about current data, not a structural guarantee -- see
-- tests/assert_stop_identity_names_are_single_valued.sql, which guards the
-- assumption rather than trusting it silently forever.
--
-- Stop attributes, taken once rather than carried through the aggregation.
select
    atco_code,
    any_value(stop_name) as stop_name,
    any_value(locality)  as locality,
    avg(stop_lat)         as lat,
    avg(stop_lon)         as lon
from {{ ref('int_pings_matched_to_stops') }}
group by atco_code
