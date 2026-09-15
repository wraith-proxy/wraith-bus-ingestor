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
-- This grain: real operator, day_type pooled as '*'. Drops rows with no
-- operator, same reasoning as int_line_stops_real_real.
select
    operator,
    line,
    direction,
    '*' as day_type,
    atco_code,
    cast(approx_quantile(secs_from_origin, 0.5) as double) as median_secs_from_origin,
    avg(secs_from_origin)                                  as mean_secs_from_origin,
    cast(count(distinct journey_key) as bigint)            as n_obs
from {{ ref('int_first_visit_per_journey') }}
where operator is not null
group by operator, line, direction, atco_code
