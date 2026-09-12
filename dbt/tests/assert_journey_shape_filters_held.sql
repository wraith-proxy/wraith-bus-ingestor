-- The plausibility filters in int_journey_shape are the contract between the
-- messy ping stream and everything downstream. If one is ever edited away, the
-- damage shows up far from the change -- as a slope of 5 s/km in a mart -- so
-- assert them where they were made.

select
    journey_key,
    n_pings,
    path_km,
    duration_secs,
    mean_speed_kmh
from {{ ref('int_journey_shape') }}
where n_pings < 5
   or path_km < 0.5
   or duration_secs < 240
   or duration_secs > 5 * 3600
   or mean_speed_kmh < 1
   or mean_speed_kmh > 90
