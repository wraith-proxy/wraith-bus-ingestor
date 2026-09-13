{#
  The place-name index behind GET /api/search.

  One row per normalised locality, with a point to fly the map to and a rough
  size so the API can prefer "Manchester" over a hamlet that happens to share a
  prefix. Built from stops rather than from any gazetteer, so the only places
  that exist here are places that actually have bus stops -- which is the only
  kind of place this application can say anything about.
#}

with stops as (

    select * from {{ ref('stg_naptan__stops') }}

),

by_locality as (

    select
        locality_norm,
        -- The normalised key is what we match on, but the user should be shown
        -- a human spelling. mode() picks the most common raw form, so
        -- "Ashton-under-Lyne" wins over a stray "ASHTON UNDER LYNE".
        mode(locality)          as locality,
        -- Centroid of the locality's stops. Not the same as the centroid of the
        -- settlement -- it is pulled towards wherever the buses go -- which is
        -- the more useful place to drop the map pin anyway.
        avg(lat)                as lat,
        avg(lon)                as lon,
        cast(count(*) as bigint) as n_stops
    from stops
    group by locality_norm

)

select
    locality_norm,
    locality,
    lat,
    lon,
    n_stops
from by_locality
