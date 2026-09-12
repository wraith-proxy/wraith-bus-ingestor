-- The API's model lookup walks six rungs from
-- (line, direction, day_type, hour_band) out to ('*','*','*','*') and must be
-- guaranteed a hit at the end of that walk. If the
-- global row is ever missing -- because the sanity filters rejected it, or
-- because the training set came out empty after an extract failure -- every
-- bus on the map silently loses its ETA.
--
-- A singular test is just a query that must return no rows. This one returns a
-- row when the global fallback is absent.

select
    'mart_eta_model is missing the global (*,*,*,*) fallback row' as failure

from (select 1) as always_one_row

where not exists (
    select 1
    from {{ ref('mart_eta_model') }}
    where line = '*'
      and direction = '*'
      and day_type = '*'
      and hour_band = '*'
)
