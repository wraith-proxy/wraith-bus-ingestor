-- Composite primary key of mart_line_localities.

select
    line,
    locality_norm,
    count(*) as n_rows
from {{ ref('mart_line_localities') }}
group by line, locality_norm
having count(*) > 1
