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
        {{ locality_norm('locality') }} as locality_norm
    from typed
    -- A stop with no coordinates cannot be matched to a ping, and a couple of
    -- hundred NaPTAN rows carry blank or non-numeric Easting/Northing.
    where lat is not null
      and lon is not null
      and {{ in_gb_bbox('lat', 'lon') }}
      and locality is not null

)

select * from final
