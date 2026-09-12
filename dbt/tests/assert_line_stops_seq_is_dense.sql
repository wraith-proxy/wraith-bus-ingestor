-- seq must be 1..n with no gaps and no repeats within each
-- (operator, line, direction, day_type).
-- The API walks the sequence to find "the stop after this one"; a gap makes it
-- skip a stop, a repeat makes it loop.

with per_line_direction as (

    select
        operator,
        line,
        direction,
        day_type,
        count(*)              as n_stops,
        count(distinct seq)   as n_distinct_seq,
        min(seq)              as min_seq,
        max(seq)              as max_seq
    from {{ ref('mart_line_stops') }}
    group by operator, line, direction, day_type

)

select *
from per_line_direction
where min_seq <> 1
   or max_seq <> n_stops
   or n_distinct_seq <> n_stops
