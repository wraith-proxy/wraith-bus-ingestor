-- Builds the two serving tables that replace the line_stops / line_localities
-- marts, from the raw gtfs_* tables and naptan_stops that fetch.py has loaded
-- into the same connection. Executed as one script; every intermediate is a
-- TEMP table so it is computed once and never bloats the staging db file.

-- GTFS times are 'HH:MM:SS' with HH allowed past 24 ("25:10:00" = 01:10 next
-- day). A TIME cast rejects those, so do integer arithmetic on the parts.
-- Empty string -> NULL (BODS leaves times blank on some non-timing stops).
CREATE OR REPLACE TEMP MACRO gtfs_secs(t) AS
    CASE WHEN nullif(trim(t), '') IS NULL THEN NULL
         ELSE try_cast(split_part(t, ':', 1) AS INTEGER) * 3600
            + try_cast(split_part(t, ':', 2) AS INTEGER) * 60
            + try_cast(split_part(t, ':', 3) AS INTEGER)
    END;

-- Same rule as the dbt locality_norm macro (frozen in CONTRACT.md s2):
-- uppercase, non-alphanumerics to space, collapse runs, trim. Keep in step
-- with dbt/macros/locality_norm.sql or the search keys stop matching.
CREATE OR REPLACE TEMP MACRO gtfs_locality_norm(x) AS
    trim(regexp_replace(upper(regexp_replace(x, '[^A-Za-z0-9 ]', ' ', 'g')), ' +', ' ', 'g'));

-- Services that have not expired as of the refresh date. Deliberately NOT
-- "runs today": a line that does not run on a Sunday still has a route.
-- calendar dates arrive as YYYYMMDD; read_csv may have typed them as integer
-- or DATE, so cast via varchar and accept both spellings.
CREATE OR REPLACE TEMP TABLE _live_services AS
SELECT DISTINCT service_id
FROM (
    SELECT cast(service_id AS VARCHAR) AS service_id,
           coalesce(try_strptime(cast(end_date AS VARCHAR), '%Y%m%d'),
                    try_cast(cast(end_date AS VARCHAR) AS TIMESTAMP)) AS end_date
    FROM gtfs_calendar
)
WHERE end_date >= current_date
UNION
-- exception_type 1 = service added on that date; a service that only exists
-- as added dates has no calendar row at all.
SELECT DISTINCT cast(service_id AS VARCHAR)
FROM gtfs_calendar_dates
WHERE cast(exception_type AS VARCHAR) = '1'
  AND coalesce(try_strptime(cast(date AS VARCHAR), '%Y%m%d'),
               try_cast(cast(date AS VARCHAR) AS TIMESTAMP)) >= current_date;

-- One row per live trip, with operator/line/direction attached.
CREATE OR REPLACE TEMP TABLE _live_trips AS
SELECT
    cast(t.trip_id AS VARCHAR)                 AS trip_id,
    a.agency_noc                               AS operator,
    r.route_short_name                         AS line,
    -- !! DIRECTION MAPPING IS A DESIGN CHOICE, NOT A FACT !!
    -- The live feed's vocabulary (line_stops.direction) is
    -- outbound / inbound / clockwise / anticlockwise. GTFS only has
    -- direction_id 0/1, meaning "one way / the opposite way" with no promise
    -- that 0 is what SIRI-VM calls outbound. We map 0 -> outbound, 1 -> inbound.
    -- UNCERTAIN: (a) an operator may have 0 and 1 swapped relative to its
    -- SIRI DirectionRef, so the API must not assume a GTFS 'outbound' trip
    -- matches a live 'outbound' vehicle without checking; (b) circular routes
    -- may carry clockwise/anticlockwise: stg_bods__vehicle_pings passes a
    -- feed-supplied DirectionRef of that kind through, although
    -- int_journeys_shaped_and_directed maps inferred circular directions onto
    -- outbound/inbound. Which of these line_stops.direction actually holds has
    -- NOT been measured (no row count taken); check before relying on a join.
    -- GTFS cannot say clockwise vs anticlockwise, so circular routes come out
    -- here as outbound/inbound.
    -- NULL / blank / unparseable direction_id: treated as 'outbound'. Feeds
    -- that omit direction_id are almost always single-direction listings, and
    -- dropping them would lose the route. Where a line mixes NULL and 0 trips
    -- they merge into one 'outbound' pattern.
    CASE try_cast(t.direction_id AS INTEGER)
        WHEN 1 THEN 'inbound'
        ELSE 'outbound'
    END                                        AS direction
FROM gtfs_trips t
JOIN _live_services s ON s.service_id = cast(t.service_id AS VARCHAR)
JOIN gtfs_routes r    ON cast(r.route_id AS VARCHAR) = cast(t.route_id AS VARCHAR)
-- One noc per agency_id: a duplicated agency.txt row would fan out trips and
-- double n_trips. ponytail: a route with a blank/NULL agency_id is dropped
-- (no single-agency fallback); fetch.py should compare live trips before and
-- after this join if that matters.
JOIN (SELECT cast(agency_id AS VARCHAR) AS agency_id, min(agency_noc) AS agency_noc
      FROM gtfs_agency GROUP BY 1) a ON a.agency_id = cast(r.agency_id AS VARCHAR)
WHERE nullif(trim(a.agency_noc), '') IS NOT NULL
  AND nullif(trim(r.route_short_name), '') IS NOT NULL;

-- Stop ids as dense integers, so the big table below carries 4-byte keys, not strings.
CREATE OR REPLACE TEMP TABLE _stop_ix AS
SELECT stop_id, cast(row_number() OVER (ORDER BY stop_id) AS INTEGER) AS ix
FROM (SELECT DISTINCT stop_id FROM gtfs_stop_times);

-- The live trips' stop_times with each stop's position in its trip. Rows with a
-- NULL stop_sequence cannot be placed and are dropped; ties on stop_sequence
-- break on the stop (a total order, so pos is deterministic). arr falls back to
-- departure where blank (first stops often only carry a departure); dep is the
-- mirror, and the first stop's dep is the trip's origin clock.
-- Why a window and not list(stop ORDER BY seq) per trip: the ordered list
-- aggregate buffers every row of every group and DuckDB cannot spill it; it
-- outgrew a 4GB memory_limit on 59M rows. A window sort spills.
CREATE OR REPLACE TEMP TABLE _live_st AS
SELECT lt.trip_id, x.ix,
       row_number() OVER (PARTITION BY lt.trip_id ORDER BY st.stop_sequence, x.ix) AS pos,
       coalesce(gtfs_secs(st.arrival_time), gtfs_secs(st.departure_time)) AS arr,
       coalesce(gtfs_secs(st.departure_time), gtfs_secs(st.arrival_time)) AS dep
FROM _live_trips lt
JOIN gtfs_stop_times st ON st.trip_id = lt.trip_id
JOIN _stop_ix x ON x.stop_id = st.stop_id
WHERE st.stop_sequence IS NOT NULL;

-- One row per trip, identified by a fingerprint of its ordered stop list: the
-- sum of hash(stop, position) over its stops, kept as HUGEINT so the sum cannot
-- wrap. Two trips share a fingerprint iff they visit the same stops in the same
-- order (a 64-bit hash per term; a collision needs two different patterns of
-- one line and direction to cancel exactly, which we accept). This avoids
-- materialising a list per trip.
-- ponytail: no collision check; the ceiling is one wrong pattern in ~2^64.
CREATE OR REPLACE TEMP TABLE _trip_sig AS
SELECT lt.operator, lt.line, lt.direction, s.trip_id, s.fp, s.first_dep
FROM (SELECT trip_id,
             sum(hash(ix, pos)::HUGEINT)             AS fp,
             max(dep) FILTER (WHERE pos = 1)         AS first_dep
      FROM _live_st GROUP BY trip_id) s
JOIN _live_trips lt USING (trip_id);

-- Trips per distinct pattern, ranked within each (operator, line, direction).
-- rank 1 = primary: most trips, ties broken on the fingerprint so a re-run on
-- the same feed numbers the patterns identically. trip_share is taken over ALL
-- the key's trips, before pruning.
CREATE OR REPLACE TEMP TABLE _patterns AS
SELECT operator, line, direction, fp, n_trips,
       row_number() OVER (PARTITION BY operator, line, direction
                          ORDER BY n_trips DESC, fp)                    AS rn,
       n_trips / sum(n_trips) OVER (PARTITION BY operator, line, direction) AS trip_share
FROM (SELECT operator, line, direction, fp, count(*) AS n_trips
      FROM _trip_sig
      GROUP BY operator, line, direction, fp);

-- PRUNING RULE (decided): keep a pattern if it is the primary OR ran >= 2 trips.
-- One-off patterns are dropped. Because rank is by n_trips DESC, the survivors
-- are always ranks 1..k with no gap, so rn is already the dense pattern_id.
-- pattern_id is NOT stable across refreshes: never cache it across a db swap.
CREATE OR REPLACE TEMP TABLE _kept AS
SELECT operator, line, direction, fp, n_trips, rn AS pattern_id, trip_share
FROM _patterns
WHERE rn = 1 OR n_trips >= 2;

-- Median scheduled offset per position along each kept pattern, over that
-- pattern's own trips.
CREATE OR REPLACE TEMP TABLE _kept_offsets AS
SELECT k.operator, k.line, k.direction, k.pattern_id, l.pos,
       any_value(k.n_trips) AS n_trips,
       any_value(x.stop_id) AS stop_id,
       median(l.arr - ts.first_dep) AS sched_secs_from_origin
FROM _kept k
JOIN _trip_sig ts USING (operator, line, direction, fp)
JOIN _live_st l ON l.trip_id = ts.trip_id
JOIN _stop_ix x ON x.ix = l.ix
GROUP BY k.operator, k.line, k.direction, k.pattern_id, l.pos;

-- NaPTAN locality, one row per stop (guards against a duplicated ATCOCode
-- fanning out the join). Same coalesce order as stg_naptan__stops.sql:
-- LocalityName, then Town, then ParentLocalityName. No bus-type/status filter
-- here: a stop a timetable serves should get a locality if NaPTAN has one.
CREATE OR REPLACE TEMP TABLE _naptan_loc AS
SELECT cast(ATCOCode AS VARCHAR) AS atco_code,
       cast(CommonName AS VARCHAR) AS stop_name,
       try_cast(Latitude AS DOUBLE) AS lat,
       try_cast(Longitude AS DOUBLE) AS lon,
       coalesce(
           nullif(trim(cast(LocalityName AS VARCHAR)), ''),
           nullif(trim(cast(Town AS VARCHAR)), ''),
           nullif(trim(cast(ParentLocalityName AS VARCHAR)), '')
       ) AS locality
FROM naptan_stops
QUALIFY row_number() OVER (PARTITION BY cast(ATCOCode AS VARCHAR)
                           ORDER BY lower(cast(Status AS VARCHAR)) = 'active' DESC,
                                    cast(ATCOCode AS VARCHAR)) = 1;

DROP TABLE IF EXISTS gtfs_route_stops;
CREATE TABLE gtfs_route_stops AS
SELECT
    o.operator,
    o.line,
    o.direction,
    o.stop_id                                            AS atco_code,
    coalesce(nullif(trim(s.stop_name), ''), n.stop_name) AS stop_name,
    n.locality                                           AS locality,   -- NULL if not in NaPTAN; row kept
    coalesce(try_cast(s.stop_lat AS DOUBLE), n.lat)      AS lat,
    coalesce(try_cast(s.stop_lon AS DOUBLE), n.lon)      AS lon,
    -- pos is already dense 1..n along the pattern; a looped stop keeps both
    -- occurrences because the position, not the stop, is the key.
    cast(o.pos AS INTEGER)                               AS seq,
    cast(o.sched_secs_from_origin AS DOUBLE)             AS sched_secs_from_origin,
    cast(o.n_trips AS BIGINT)                            AS n_trips,
    cast(o.pattern_id AS BIGINT)                         AS pattern_id,
    o.pattern_id = 1                                     AS is_primary
FROM _kept_offsets o
LEFT JOIN (SELECT DISTINCT ON (cast(stop_id AS VARCHAR))
                  cast(stop_id AS VARCHAR) AS stop_id, stop_name, stop_lat, stop_lon
           FROM gtfs_stops ORDER BY cast(stop_id AS VARCHAR)) s ON s.stop_id = o.stop_id
LEFT JOIN _naptan_loc n ON n.atco_code = o.stop_id
-- Sorted so DuckDB zone maps prune a per-route lookup.
ORDER BY o.operator, o.line, o.direction, o.pattern_id, o.pos;

-- One row per kept pattern. first/last stop let a live bus's origin_ref /
-- dest_ref pin a pattern. GTFS never says clockwise/anticlockwise but the live
-- feed does, so for a circular pattern (first = last) the API must ignore the
-- direction label.
DROP TABLE IF EXISTS gtfs_route_patterns;
CREATE TABLE gtfs_route_patterns AS
SELECT k.operator, k.line, k.direction,
       cast(k.pattern_id AS BIGINT)        AS pattern_id,
       k.pattern_id = 1                    AS is_primary,
       cast(k.n_trips AS BIGINT)           AS n_trips,
       cast(e.n_stops AS INTEGER)          AS n_stops,
       e.first_atco, e.last_atco,
       e.first_atco = e.last_atco          AS is_circular,
       cast(k.trip_share AS DOUBLE)        AS trip_share
FROM _kept k
JOIN (SELECT operator, line, direction, pattern_id,
             count(*)                        AS n_stops,
             arg_min(atco_code, seq)         AS first_atco,
             arg_max(atco_code, seq)         AS last_atco
      FROM gtfs_route_stops
      GROUP BY operator, line, direction, pattern_id) e
  USING (operator, line, direction, pattern_id)
ORDER BY k.operator, k.line, k.direction, k.pattern_id;

-- Distinct-stop counts over ALL kept patterns' stops.
DROP TABLE IF EXISTS gtfs_line_localities;
CREATE TABLE gtfs_line_localities AS
SELECT line,
       gtfs_locality_norm(locality) AS locality_norm,
       min(locality)                AS locality,   -- min() only to be deterministic when spellings differ
       count(DISTINCT atco_code)    AS n_stops
FROM gtfs_route_stops
WHERE nullif(gtfs_locality_norm(locality), '') IS NOT NULL
GROUP BY line, gtfs_locality_norm(locality)
ORDER BY line, locality_norm;

-- Kept (TEMP, dies with the connection) so the final SELECT can return it.
-- Measure with: SELECT * FROM _pattern_stats  (run before the DROPs below).
-- Measured 2026-09-20 on the real BODS feed: 51,446 distinct patterns, 42,009
-- kept (99.4% of trips; primaries alone cover 80.6%). gtfs_route_stops
-- 1,499,050 rows, gtfs_route_patterns 42,009 rows, 8,185 keys with >1 pattern.
CREATE OR REPLACE TEMP TABLE _pattern_stats AS
SELECT count(*)                                                         AS distinct_patterns,
       count(*) FILTER (WHERE rn = 1 OR n_trips >= 2)                   AS kept_patterns,
       sum(n_trips) FILTER (WHERE rn = 1 OR n_trips >= 2) / sum(n_trips) AS kept_trip_share,
       sum(n_trips) FILTER (WHERE rn = 1) / sum(n_trips)                AS primary_trip_share
FROM _patterns;

DROP TABLE _live_services;
DROP TABLE _live_trips;
DROP TABLE _live_st;
DROP TABLE _trip_sig;
DROP TABLE _stop_ix;
DROP TABLE _patterns;
DROP TABLE _kept;
DROP TABLE _kept_offsets;
DROP TABLE _naptan_loc;

-- Last statement: con.execute(sql_text).fetchall() yields this one row:
-- (distinct_patterns, kept_patterns, kept_trip_share, primary_trip_share).
SELECT * FROM _pattern_stats;
