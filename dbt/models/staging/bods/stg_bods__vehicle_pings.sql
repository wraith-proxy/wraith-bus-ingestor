{#
  One row per bus per 3-minute bucket: the atom every other model is built from.
  Rename + cast + cheap derivations only. No joins, no aggregation, no filtering
  beyond "this row is not physically possible".

  Every cast here is a try_cast, deliberately. extract.py now pins an explicit
  column list so every daily file shares a schema, but it did not always: a day
  on which QuestDB held no rows used to auto-detect every column as VARCHAR,
  including the timestamp, and under union_by_name one such file dragged the
  whole glob down to VARCHAR. try_cast costs nothing when the types are already
  right and turns a repeat of that into a non-event rather than a build
  failure. A value that genuinely will not parse becomes NULL and is dropped by
  the `plausible` filter below, which is the behaviour we want for a GPS feed.

  NOTE ON DATE COVERAGE: the extract window is NOT contiguous. The feed had an
  outage over 2026-08-28..08-30, so those days have no file at all. Nothing in
  this project may assume consecutive dates -- int_pings_segmented_to_journeys
  breaks a journey on any gap over 20 minutes, so a four-day hole is handled by
  the same rule that handles a bus going into the depot, and
  int_journeys_shaped_and_directed separately discards journeys whose end falls
  inside a coverage hole.
#}

{#-
  The schedule columns only exist in parquet files written after the ingestor
  patch (2026-09-12). ping_columns() asks DuckDB what the file union actually
  has; anything missing is emitted as a typed NULL so downstream SQL compiles
  either way. See macros/ping_columns.sql.
-#}
{%- set available = ping_columns() -%}
{%- set optional = [
     ('direction',        'varchar'),
     ('operator',         'varchar'),
     ('journey_ref',      'varchar'),
     ('origin_ref',       'varchar'),
     ('origin_name',      'varchar'),
     ('dest_ref',         'varchar'),
     ('dest_name',        'varchar'),
     ('origin_aimed_dep', 'bigint'),
     ('dest_aimed_arr',   'bigint'),
     ('recorded_at',      'bigint')
   ] -%}

with source as (

    select
        -- Normalise the instant first, then derive both clocks from it below.
        try_cast("timestamp" as timestamptz) as ping_tstz,
        cast(bus_id as varchar)              as bus_id,
        cast(line as varchar)                as line,
        try_cast(lat as double)              as lat,
        try_cast(lon as double)              as lon,
        try_cast(bearing as double)          as bearing
        {%- for col, type in optional %},
        {% if col in available %}try_cast({{ col }} as {{ type }}){% else %}cast(null as {{ type }}){% endif %} as {{ col }}
        {%- endfor %}
    from {{ source('bods', 'bus_positions') }}

),

plausible as (

    -- A vehicle whose GPS has not fixed reports (0, 0) or a stale foreign
    -- position. Those rows would otherwise dominate the haversine path length
    -- of a journey (a single Null Island ping adds ~5,500 km).
    -- Bounding box is Great Britain incl. Scilly and Shetland.
    select *
    from source
    where ping_tstz is not null
      and lat is not null
      and lon is not null
      and lat between 49.8 and 61.0
      and lon between -8.7 and 2.0
      and not (lat = 0 and lon = 0)

),

in_scope as (

    -- UK bank holidays run a Sunday-ish timetable but fall on a Monday, so the
    -- day_type rule (which is pure calendar arithmetic, by design -- see
    -- macros/day_type.sql) files them as 'weekday' and they pollute the weekday
    -- grain with weekend-shaped running.
    --
    -- For a single known holiday inside the extract window that is cheap to
    -- just exclude, and doing so is honest: we are not modelling bank holidays,
    -- so the data should not pretend we are. This is NOT a holiday calendar and
    -- must not grow into one -- if the window ever spans many holidays, the
    -- right answer is a proper 'holiday' day_type fed by a real calendar
    -- source, not a longer list here.
    --
    -- 2026-08-31 is the late-August bank holiday. It is also only 450k rows
    -- against a normal ~2.7M, i.e. a partial day, so it was thin evidence for
    -- anything either way.
    select *
    from plausible
    where cast(ping_tstz at time zone 'Europe/London' as date) not in (
        date '2026-08-31'
    )

),

clocked as (

    -- Both conversions are explicit, so neither depends on the session time
    -- zone of whatever box the build runs on:
    --   ping_ts        UTC wall clock -- the canonical instant, what marts publish
    --   ping_local_ts  Europe/London wall clock -- what hour_band and day_type
    --                  MUST be derived from. The UK runs UTC+1 from late March
    --                  to late October, so bucketing on UTC puts the morning
    --                  peak an hour early and files a 00:30 Saturday ping under
    --                  Friday.
    select
        *,
        ping_tstz at time zone 'UTC'           as ping_ts,
        ping_tstz at time zone 'Europe/London' as ping_local_ts
    from in_scope

),

derived as (

    select
        bus_id,
        line,
        -- The feed's own DirectionRef, normalised, or NULL when we do not have
        -- one. Deliberately NOT coalesced to '*' here: nearly the whole
        -- training set predates 2026-09-12 and would collapse into a single
        -- direction bucket, and outbound and inbound journeys would be
        -- fitted as one.
        -- int_journeys_shaped_and_directed infers a direction per journey
        -- instead, and only eta_models ever uses '*', as the name of its
        -- fallback grain.
        -- A handful of operators fill DirectionRef with their own scheme
        -- ('2', 'eastbound'); those are treated as "no usable direction".
        case lower(trim(coalesce(direction, '')))
            when 'inbound'       then 'inbound'
            when 'outbound'      then 'outbound'
            when 'clockwise'     then 'clockwise'
            when 'anticlockwise' then 'anticlockwise'
            else null
        end as feed_direction,
        operator,
        -- The feed's own per-record observation time, epoch MILLIS. Carried only
        -- as a sort tiebreak for int_pings_segmented_to_journeys (it is often
        -- stale, so it is NOT a timestamp to trust); NULL in files written
        -- before extract.py started carrying it.
        recorded_at,
        journey_ref,
        origin_ref,
        origin_name,
        dest_ref,
        dest_name,
        ping_ts,
        ping_local_ts,
        cast(ping_ts as date)                            as ping_date,
        -- Every temporal bucket below is derived from LOCAL time, never UTC.
        cast(ping_local_ts as date)                      as ping_local_date,
        cast(extract(hour from ping_local_ts) as integer) as hour_of_day,
        {{ hour_band('extract(hour from ping_local_ts)') }} as hour_band,
        {{ day_type('ping_local_ts') }}                  as day_type,
        lat,
        lon,
        bearing,
        -- SIRI aimed times arrive as epoch MILLIS (ILP long), not seconds.
        case when origin_aimed_dep is not null
             then cast(to_timestamp(origin_aimed_dep / 1000.0) as timestamp) end as origin_aimed_dep_ts,
        case when dest_aimed_arr is not null
             then cast(to_timestamp(dest_aimed_arr / 1000.0) as timestamp) end   as dest_aimed_arr_ts
    from clocked

)

select * from derived
