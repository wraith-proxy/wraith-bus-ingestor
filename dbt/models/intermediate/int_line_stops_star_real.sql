{{ config(materialized='table', contract={'enforced': true}) }}

-- One of four independent grain passes split out of line_stops.sql to fix
-- a repeated full-host OOM (see docs/ROADMAP.md's 1.1.1 entry). The old
-- version unioned first_visit_per_journey's raw rows across all four
-- operator/day_type fallback grains BEFORE aggregating, so DuckDB kept all
-- four grains' hash-aggregate state open concurrently against one
-- ~8.75M-row scan -- exact median() made that unbounded per group, and a
-- later "AS MATERIALIZED" hint inside one query was an unverified
-- assumption about scheduler behaviour, not a guarantee. Splitting into
-- four separate dbt models, run at --threads 1 (see build.sh), makes
-- "only one grain's aggregate state is ever open" a hard scheduling
-- guarantee instead of an optimizer hint.
--
-- Column list/types are enforced by the dbt contract below specifically so
-- this grain and its three siblings stay UNION-ALL-compatible with a
-- compile-time error instead of a silent runtime mismatch if one of the
-- four ever drifts.
--
-- This grain: operator pooled as '*', real day_type. No operator filter --
-- every row (with or without an operator) belongs in the pooled rungs.
select
    '*' as operator,
    line,
    direction,
    day_type,
    atco_code,
    cast(approx_quantile(secs_from_origin, 0.5) as double) as median_secs_from_origin,
    avg(secs_from_origin)                                  as mean_secs_from_origin,
    -- count(*), not count(distinct journey_key) -- see int_line_stops_real_real
    -- for why the two are provably identical here and why DISTINCT alone
    -- was measured OOM'ing a 2.2GB-capped run despite the median()/
    -- concurrency fixes already being in place.
    cast(count(*) as bigint)                               as n_obs
from {{ ref('int_first_visit_per_journey') }}
group by line, direction, day_type, atco_code
