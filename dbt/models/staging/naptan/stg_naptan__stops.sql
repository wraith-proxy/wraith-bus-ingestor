{#
  NaPTAN, narrowed to "bus stops a vehicle can actually be observed at" and
  typed. Everything downstream joins to this, never to the raw CSV.
#}

with source as (

    select * from {{ source('naptan', 'stops') }}

),

typed as (

    select
        cast(ATCOCode as varchar)            as atco_code,
        cast(CommonName as varchar)          as stop_name,
        cast(StopType as varchar)            as stop_type,
        cast(TimingStatus as varchar)        as timing_status,
        cast(Status as varchar)              as status,
        try_cast(Latitude as double)         as lat,
        try_cast(Longitude as double)        as lon,
        -- NaPTAN nests locality three deep and fills whichever level it has.
        -- Coalescing here means a stop in an unnamed hamlet still resolves to
        -- the town a user would actually type into the search box.
        coalesce(
            nullif(trim(cast(LocalityName as varchar)), ''),
            nullif(trim(cast(Town as varchar)), ''),
            nullif(trim(cast(ParentLocalityName as varchar)), '')
        )                                    as locality
    from source
    -- Bus-only stop types: BCT on-street stop, BCS marked bay, BCQ bay,
    -- BST station bay. Everything else is rail/ferry/air/metro and would put
    -- a railway station on a bus line's stop list.
    where cast(StopType as varchar) in ('BCT', 'BCS', 'BCQ', 'BST')
      -- 'active' excludes 'del' (deleted) and 'inactive' stops that no longer
      -- exist on the ground but stay in the register for historical timetables.
      and lower(cast(Status as varchar)) = 'active'

),

final as (

    select
        atco_code,
        stop_name,
        stop_type,
        timing_status,
        status,
        lat,
        lon,
        locality,
        {{ locality_norm('locality') }} as locality_norm,
        -- Integer grid cell of 0.002 deg: ~222 m north-south, ~131 m east-west
        -- at UK latitudes. Precomputed here so int_pings_matched_to_stops can
        -- equi-join on it instead of cross joining 20M pings against 344k stops.
        -- Integers, not round(lat,3), so that "the cell one to the east" is
        -- exactly cell+1 and never a float that misses the join by 1e-15.
        cast(floor(lat * 500) as integer) as lat_cell,
        cast(floor(lon * 500) as integer) as lon_cell
    from typed
    -- A stop with no coordinates cannot be matched to a ping, and a couple of
    -- hundred NaPTAN rows carry blank or non-numeric Easting/Northing.
    where lat is not null
      and lon is not null
      and lat between 49.8 and 61.0
      and lon between -8.7 and 2.0
      and locality is not null

)

select * from final
