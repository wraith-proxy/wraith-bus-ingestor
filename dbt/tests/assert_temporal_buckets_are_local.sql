-- hour_band and day_type must be derived from Europe/London local time, not
-- from the UTC instant.
--
-- This is a bug that leaves no trace: everything still builds, every value is
-- still a legal band name, and the model is simply fitted against the wrong
-- hour. The UK runs UTC+1 from late March to late October, so during BST a
-- UTC-derived bucket is one hour early -- the morning peak lands in the
-- off-peak, and a 00:30 Saturday ping files under Friday.
--
-- Re-deriving both from ping_local_ts and comparing catches any future edit
-- that reverts to ping_ts, including one made in a macro rather than here.

select
    ping_ts,
    ping_local_ts,
    hour_of_day,
    hour_band,
    day_type
from {{ ref('stg_bus_pings') }}
where hour_of_day <> extract(hour from ping_local_ts)
   or hour_band   <> ({{ hour_band('extract(hour from ping_local_ts)') }})
   or day_type    <> ({{ day_type('ping_local_ts') }})
   -- and the local clock must actually differ from UTC when BST is in force
   or (cast(ping_ts as date) between '2026-03-30' and '2026-10-24'
       and ping_local_ts = ping_ts)
