-- Every (line, direction, day_type) that has a real-operator stop sequence
-- must also have one at operator = '*'.
--
-- The API prefers a live bus's own operator and falls back to '*' when that
-- operator has no fitted sequence (or the bus has no operator at all, true of
-- every ping before 2026-09-12). Without the fallback rung, the mart's
-- per-operator rows would be strictly additive risk: a line whose one known
-- operator drops out of a rebuild's observation window would go from "has a
-- sequence" to "has none" instead of degrading to the pooled one.

with real_grains as (

    select distinct line, direction, day_type
    from {{ ref('mart_line_stops') }}
    where operator <> '*'

),

fallback_grains as (

    select distinct line, direction, day_type
    from {{ ref('mart_line_stops') }}
    where operator = '*'

)

select
    r.line,
    r.direction,
    r.day_type
from real_grains r
left join fallback_grains f
    on r.line = f.line
   and r.direction = f.direction
   and r.day_type = f.day_type
where f.line is null
