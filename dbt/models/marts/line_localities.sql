{#
  Which places each line actually serves, derived from where its buses were
  seen rather than from any published route description.

  This is the join that makes "search WILMSLOW, highlight the buses" work: the
  API resolves the typed text to a locality_norm in localities, then reads
  the lines off this table.
#}

with matches as (

    select * from {{ ref('int_pings_matched_to_stops') }}

),

by_line_locality as (

    select
        line,
        locality_norm,
        mode(locality)              as locality,
        cast(count(*) as bigint)    as n_pings,
        min(ping_ts)                as first_seen,
        max(ping_ts)                as last_seen
    from matches
    group by line, locality_norm

)

select
    line,
    locality_norm,
    locality,
    n_pings,
    first_seen,
    last_seen
from by_line_locality
-- A line genuinely serving a place is seen there hundreds of times a week. A
-- handful of pings is a GPS excursion, or a bus on a diversion, or the tail of
-- a dead-run to the depot -- and claiming "the 88 serves Stockport" on the
-- strength of one stray point is exactly the kind of wrong answer that makes a
-- search feature untrustworthy.
-- Ceiling: 20 pings, i.e. about an hour of presence across the whole window.
where n_pings >= 20
