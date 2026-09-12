-- Composite primary key of mart_eta_model. Flask indexes the model by
-- (line, direction, day_type, hour_band) and takes the first match; two rows on
-- one key would make the ETA depend on row order.
--
-- dbt's built-in `unique` test only covers a single column, and this project
-- deliberately avoids taking a dependency on dbt_utils for one macro, so the
-- composite keys are asserted as singular tests instead.

select
    line,
    direction,
    day_type,
    hour_band,
    count(*) as n_rows
from {{ ref('mart_eta_model') }}
group by line, direction, day_type, hour_band
having count(*) > 1
