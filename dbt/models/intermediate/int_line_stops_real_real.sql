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
-- This grain: real operator, real day_type. Drops rows with no operator at
-- all (everything before 2026-09-12) rather than grouping them under a
-- literal NULL -- a NULL-operator "real" grain would never be looked up by
-- anything, since a live bus either reports a real operator (and would
-- look for that) or none (and the API skips straight to the '*' rungs).
select
    operator,
    line,
    direction,
    day_type,
    atco_code,
    cast(approx_quantile(secs_from_origin, 0.5) as double) as median_secs_from_origin,
    -- Only ever used to break ties in the ordering, never published.
    -- Pings are 3-minute buckets, so secs_from_origin is quantised to
    -- multiples of 180 and a run of consecutive stops shares one median.
    -- The mean is continuous and separates them in the right order.
    avg(secs_from_origin)                                  as mean_secs_from_origin,
    cast(count(distinct journey_key) as bigint)            as n_obs
from {{ ref('int_first_visit_per_journey') }}
where operator is not null
group by operator, line, direction, day_type, atco_code
