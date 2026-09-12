-- Every (operator, line, direction) that has a stop sequence at all must also
-- have one at day_type = '*'.
--
-- The API prefers the live day pattern and falls back to '*'. Without the
-- fallback, a line that only runs Monday-Friday would return nothing at all
-- when someone opens the map on a Sunday -- and since circular routes can ONLY
-- be rated from this table, that is the difference between an ETA and an
-- 'unknown'.

with real_grains as (

    select distinct operator, line, direction
    from {{ ref('mart_line_stops') }}
    where day_type <> '*'

),

fallback_grains as (

    select distinct operator, line, direction
    from {{ ref('mart_line_stops') }}
    where day_type = '*'

)

select
    r.operator,
    r.line,
    r.direction
from real_grains r
left join fallback_grains f
    on r.operator = f.operator
   and r.line = f.line
   and r.direction = f.direction
where f.line is null
