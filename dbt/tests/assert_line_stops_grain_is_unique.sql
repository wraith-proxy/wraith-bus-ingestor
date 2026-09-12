-- Composite primary key of mart_line_stops: one row per stop per direction per
-- day pattern of a line. A duplicate would give the same stop two positions in the sequence,
-- and the API's next-stop lookup would pick between them arbitrarily.

select
    line,
    direction,
    day_type,
    atco_code,
    count(*) as n_rows
from {{ ref('mart_line_stops') }}
group by line, direction, day_type, atco_code
having count(*) > 1
