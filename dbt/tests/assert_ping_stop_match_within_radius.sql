-- Every match must be inside the 120 m radius, and each ping may claim at most
-- one stop. The grid join is the one piece of this project where an off-by-one
-- in the neighbour offsets would silently produce *fewer* matches rather than
-- an error, so it is worth asserting the invariant that survives.

select
    journey_key,
    ping_seq,
    count(*)      as n_matches,
    max(dist_km)  as worst_dist_km
from {{ ref('int_pings_matched_to_stops') }}
group by journey_key, ping_seq
having count(*) > 1
    or max(dist_km) > 0.120
