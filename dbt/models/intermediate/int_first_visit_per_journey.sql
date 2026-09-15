{{ config(materialized='table') }}

-- Promoted out of line_stops.sql (was the first_visit_per_journey CTE) so
-- the four by-grain aggregations below can each read this table directly
-- instead of every grain recomputing the same collapse-to-one-row-per-stop
-- pass on the raw pings. See int_line_stops_real_real.sql for the mart's
-- OOM history and why this split exists.
--
-- A journey usually matches the same stop on two or three consecutive
-- pings (the bus is there for longer than one 3-minute bucket), and a
-- circular route can pass the same stop twice. Collapse to one observation
-- per (journey, stop): the earliest time the journey reached it.
select
    journey_key,
    operator,
    line,
    direction,
    day_type,
    atco_code,
    min(secs_from_origin) as secs_from_origin
from {{ ref('int_pings_matched_to_stops') }}
group by journey_key, operator, line, direction, day_type, atco_code
