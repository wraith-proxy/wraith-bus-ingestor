{{ config(materialized='table') }}

{#
  Snap every ping of every surviving journey to the nearest NaPTAN bus stop
  within 120 m, or to nothing.

  ---------------------------------------------------------------------------
  THE GRID TRICK -- this is the interesting bit of the project.
  ---------------------------------------------------------------------------
  The naive way to find "the nearest stop to each ping" is a cross join:
  13 million pings x 344 thousand stops = 4.5 x 10^12 haversine evaluations.
  That is not slow, it is impossible.

  The fix is that we do not need the nearest stop out of all 344k -- we need
  the nearest stop out of the handful that are physically close enough to be
  within 120 m. So we bin both sides onto a fixed grid and let the database do
  an equi-join (a hash join, linear) instead of a comparison join (quadratic).

    cell = (floor(lat * 500), floor(lon * 500))

  0.002 degrees per cell: about 222 m north-south and about 131 m east-west at
  UK latitudes. A ping sitting at the very edge of its own cell still has the
  full width of the neighbouring cell -- 131 m -- of guaranteed coverage in
  every direction, which is why the cell size and the 120 m match radius are
  chosen together. Shrink the cells and stops just outside the radius start
  being missed; grow them and the candidate lists get longer.

  So: replicate each STOP into its own cell plus the 8 surrounding cells
  (344k x 9 = 3.1M rows, trivial), then equi-join pings to that on one integer
  pair. Any stop within 120 m of a ping is guaranteed to appear in the ping's
  own cell's candidate list, and the candidate list is a few stops long instead
  of 344 thousand. Only then do we pay for haversine, and only on the
  candidates.

  The qualify at the end keeps the single closest candidate per ping. (min_by()
  would be marginally cheaper here, but row_number() is the pattern that
  generalises to "the nearest three", so it is the one worth learning.)

  Ceiling: 120 m match radius. Tighten it if stops on opposite sides of a dual
  carriageway start cross-matching; loosen it if rural stops stop matching at
  all because the 3-minute sampling skipped straight past them.
#}

with journeys as (

    select
        journey_key,
        line,
        direction,
        day_type,
        operator,
        start_ts,
        start_ping_seq,
        end_ping_seq
    from {{ ref('int_journey_shape') }}

),

pings as (

    select
        p.journey_key,
        j.line,
        j.direction,
        -- The day pattern of the JOURNEY (the day it set off, local time), not
        -- of the individual ping. A stop sequence belongs to a whole run.
        j.day_type,
        j.operator,
        j.start_ts as journey_start_ts,
        p.ping_seq,
        p.ping_ts,
        p.lat,
        p.lon,
        cast(floor(p.lat * 500) as integer) as lat_cell,
        cast(floor(p.lon * 500) as integer) as lon_cell
    from {{ ref('int_bus_journeys') }} p
    join journeys j using (journey_key)
    where p.ping_seq between j.start_ping_seq and j.end_ping_seq

),

-- Each stop, repeated into the 3x3 block of cells around it. Written as a
-- cross join against a nine-row literal table rather than nine UNIONed
-- selects, so the stop list is scanned once.
neighbour_offsets as (

    select * from (values (-1,-1),(-1,0),(-1,1),
                          ( 0,-1),( 0,0),( 0,1),
                          ( 1,-1),( 1,0),( 1,1)) as t(d_lat, d_lon)

),

stops_spread as (

    select
        s.atco_code,
        s.stop_name,
        s.locality,
        s.locality_norm,
        s.lat,
        s.lon,
        s.lat_cell + o.d_lat as lat_cell,
        s.lon_cell + o.d_lon as lon_cell
    from {{ ref('stg_naptan_stops') }} s
    cross join neighbour_offsets o

),

candidates as (

    select
        p.journey_key,
        p.line,
        p.direction,
        p.day_type,
        p.operator,
        p.journey_start_ts,
        p.ping_seq,
        p.ping_ts,
        s.atco_code,
        s.stop_name,
        s.locality,
        s.locality_norm,
        s.lat as stop_lat,
        s.lon as stop_lon,
        {{ haversine_km('p.lat', 'p.lon', 's.lat', 's.lon') }} as dist_km
    from pings p
    join stops_spread s
      on p.lat_cell = s.lat_cell
     and p.lon_cell = s.lon_cell

),

nearest as (

    select *
    from candidates
    where dist_km <= 0.120
    -- One winner per ping. (journey_key, ping_seq) is the ping's identity.
    qualify row_number() over (
        partition by journey_key, ping_seq order by dist_km, atco_code
    ) = 1

)

select
    journey_key,
    line,
    direction,
    day_type,
    operator,
    journey_start_ts,
    ping_seq,
    ping_ts,
    atco_code,
    stop_name,
    locality,
    locality_norm,
    stop_lat,
    stop_lon,
    dist_km,
    date_diff('second', journey_start_ts, ping_ts) as secs_from_origin
from nearest
