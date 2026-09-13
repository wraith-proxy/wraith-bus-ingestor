-- Every published fit must imply a speed a bus could actually achieve.
--
-- This is the assertion that catches a broken journey segmentation. If
-- int_pings_segmented_to_journeys glues two runs together, remaining_km inflates and the
-- implied speed shoots past 90 km/h; if it splits a journey into stationary
-- fragments, the implied speed collapses below 3 km/h. eta_models already
-- filters on exactly this, so the test is really checking that the filter is
-- still wired up -- which is the point: a silently-removed guard is worse than
-- one that was never there.
--
-- 3 km/h: walking pace, i.e. a parked bus with drifting GPS.
-- 90 km/h: faster than any stopping bus service averages.

select
    line,
    direction,
    hour_band,
    slope_s_per_km,
    mean_speed_kmh,
    n_obs
from {{ ref('eta_models') }}
where slope_s_per_km is null
   or slope_s_per_km <= 0
   or mean_speed_kmh is null
   or mean_speed_kmh < 3
   or mean_speed_kmh > 90
   -- mean_speed_kmh must stay the published form of the slope, not drift from it.
   or abs(mean_speed_kmh - 3600.0 / slope_s_per_km) > 1e-6
