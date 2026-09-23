"""Read-only lookups against the served GTFS database (gtfs/fetch.py builds it).

Replaces the line_stops / line_localities marts (ADR-0008). Three rules:

* Never hold a connection. Every query opens the file read-only and closes it,
  like the marts loader, so a weekly refresh swapping the file is never blocked.
* Nothing is loaded up front. The database is 1.1 GB and gtfs_route_stops alone
  is 1.5M rows; loading it into Python is exactly what made the API need 6 GB
  for line_stops. Lookups are lazy and cached per key.
* Every cache is dropped when the file's mtime moves, because pattern_id is only
  stable within one build. A missing or unreadable file degrades to "no answer",
  never an exception.

Coverage caveat: BODS GTFS has no TfL (TFLO) timetable, so routes and line
localities are empty for London. Stop coordinates come from NaPTAN, which does
cover London, so the destination-distance ETA and geometric delay still work
there.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

try:  # optional at import time, same as app.py
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

try:  # same fallback as app.py: tzdata may be missing from a slim image
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo('Europe/London')
except Exception:  # pragma: no cover
    _LOCAL_TZ = timezone.utc

GTFS_PATH = os.getenv('GTFS_PATH', '/gtfs/gtfs.duckdb')
GTFS_TTL = 60                # only stat the file once a minute

# app.py sets this to its display_name so stop names read the same as before.
name_fn = lambda s: s  # noqa: E731

_lock = threading.Lock()
_state = {'mtime': None, 'checked': 0.0}
_stops = {}                  # atco -> stop dict, or None for a known miss
_cands = {}                  # (operator, line, origin, dest) -> [pattern dicts]
_lines = {}                  # (operator, line, direction) -> route dict | None
_pins = {}                   # (operator, line, origin, aimed_dep_ms) -> pin dict | None
generation = 0               # bumps when the file changes; pattern ids die with it


def _clear():
    global generation
    generation += 1
    _stops.clear()
    _cands.clear()
    _lines.clear()
    _pins.clear()


def _check():
    """Drop every cache if the served file changed; at most one stat a minute."""
    now = time.time()
    if now - _state['checked'] < GTFS_TTL:
        return
    with _lock:
        if now - _state['checked'] < GTFS_TTL:
            return
        _state['checked'] = now
        try:
            mtime = os.path.getmtime(GTFS_PATH) if os.path.isfile(GTFS_PATH) else None
        except OSError:
            mtime = None
        if mtime != _state['mtime']:
            _clear()
            _state['mtime'] = mtime


def available():
    _check()
    return _state['mtime'] is not None and duckdb is not None


def _query(sql, params=()):
    """Rows, or [] on any failure. The connection is closed before returning."""
    if not available():
        return []
    try:
        con = duckdb.connect(GTFS_PATH, read_only=True)
    except Exception:
        return []
    try:
        return con.execute(sql, list(params)).fetchall()
    except Exception:
        return []
    finally:
        con.close()


def _clean(text):
    """A field with the two separator control characters removed."""
    return str(text).replace(chr(30), '').replace(chr(31), '')


def _pack(items):
    """Many strings as ONE bound parameter, for `unnest(string_split(?, chr(30)))`.
    Binding a Python list makes DuckDB do a failed import lookup per element,
    which cost 6 s for a poll's worth of keys."""
    return chr(30).join(items)


def _stop_dict(atco, name, locality, lat, lon, seq=None, secs=None):
    return {'atco': atco, 'name': name_fn(name), 'locality': locality,
            'lat': lat, 'lon': lon, 'seq': seq, 'secs_from_origin': secs}


def prime(atcos):
    """Fill the stop cache for many ATCO codes in one query (one poll's worth)."""
    _check()
    want = sorted({a for a in atcos if a and a not in _stops})
    if not want:
        return
    rows = _query(
        'SELECT ATCOCode, CommonName, LocalityName, try_cast(Latitude AS DOUBLE), '
        'try_cast(Longitude AS DOUBLE) FROM naptan_stops '
        'WHERE ATCOCode IN (SELECT unnest(string_split(?, chr(30)))) '
        'ORDER BY (Status = \'active\')', [_pack(_clean(a) for a in want)])
    for atco, name, loc, lat, lon in rows:      # active rows come last and win
        if lat is not None and lon is not None:
            _stops[atco] = _stop_dict(atco, name, loc, lat, lon)
    for atco in want:
        _stops.setdefault(atco, None)


def stop(atco):
    """{'atco','name','locality','lat','lon',...} for an ATCO code, or None."""
    if not atco:
        return None
    _check()
    if atco not in _stops:
        prime([atco])
    return _stops.get(atco)


def _load_candidates(keys):
    """Fill _cands for many (operator, line, origin_ref, dest_ref) keys with three
    queries in one connection, not three per key: a cold poll has over a thousand
    keys, and one query each would take longer than the request budget.

    The live (operator, line) is taken as the timetable spells it if the
    timetable has it, else through the reviewed alias table (which never shadows
    a real match; checking exact first keeps it so)."""
    if not available():
        return
    try:
        con = duckdb.connect(GTFS_PATH, read_only=True)
    except Exception:
        return
    found = {k: [] for k in keys}
    try:
        con.execute('CREATE TEMP TABLE k(op VARCHAR, ln VARCHAR, o VARCHAR, d VARCHAR)')
        # One statement, one bound string: executemany is row-at-a-time (7 s for 9k
        # keys) and so is binding a list parameter (a failed module lookup per
        # element, 6 s). Split in SQL instead.
        con.execute(
            'INSERT INTO k SELECT split_part(t, chr(31), 1), split_part(t, chr(31), 2), '
            'split_part(t, chr(31), 3), split_part(t, chr(31), 4) '
            'FROM (SELECT unnest(string_split(?, chr(30))) AS t)',
            [_pack(chr(31).join(_clean(f) for f in k) for k in keys)])
        con.execute(
            'CREATE TEMP TABLE kr AS SELECT k.*, '
            'CASE WHEN e.operator IS NOT NULL THEN k.op ELSE a.gtfs_operator END AS rop, '
            'CASE WHEN e.operator IS NOT NULL THEN k.ln ELSE a.gtfs_line END AS rln '
            'FROM k LEFT JOIN (SELECT DISTINCT operator, line FROM gtfs_route_patterns) e '
            'ON e.operator = k.op AND e.line = k.ln '
            'LEFT JOIN gtfs_line_alias a ON a.live_operator = k.op AND a.live_line = k.ln')
        pats = con.execute(
            'SELECT kr.op, kr.ln, kr.o, kr.d, p.operator, p.line, p.direction, '
            'p.pattern_id, p.trip_share, p.is_circular '
            'FROM kr JOIN gtfs_route_patterns p ON p.operator = kr.rop AND p.line = kr.rln '
            'AND p.first_atco = kr.o AND p.last_atco = kr.d '
            'ORDER BY p.trip_share DESC, p.pattern_id').fetchall()
        stops = {}
        for r in con.execute(
                'SELECT s.operator, s.line, s.direction, s.pattern_id, s.atco_code, '
                's.stop_name, s.locality, s.lat, s.lon, s.seq, s.sched_secs_from_origin '
                'FROM gtfs_route_stops s JOIN (SELECT DISTINCT p.operator, p.line, '
                'p.direction, p.pattern_id FROM kr JOIN gtfs_route_patterns p '
                'ON p.operator = kr.rop AND p.line = kr.rln AND p.first_atco = kr.o '
                'AND p.last_atco = kr.d) u ON u.operator = s.operator AND u.line = s.line '
                'AND u.direction = s.direction AND u.pattern_id = s.pattern_id '
                'ORDER BY s.seq').fetchall():
            stops.setdefault(r[:4], []).append(
                _stop_dict(r[4], r[5], r[6], r[7], r[8], seq=r[9], secs=r[10]))
    except Exception:
        return                          # leave the keys uncached: retried next poll
    finally:
        con.close()
    for op, ln, o, d, rop, rln, direction, pid, share, circ in pats:
        found[(op, ln, o, d)].append({
            'pattern_id': pid, 'direction': direction, 'trip_share': share,
            'is_circular': bool(circ), 'stops': stops.get((rop, rln, direction, pid), [])})
    _cands.update(found)


def prime_routes(keys):
    """Batch-load candidates for the (operator, line, origin_ref, dest_ref) keys
    of one poll; keys already cached, or missing a field, are skipped."""
    _check()
    todo = sorted({k for k in keys if all(k) and k not in _cands})
    if todo:
        _load_candidates(todo)


def candidates(operator, line, origin_ref, dest_ref):
    """Every timetable pattern of this line that runs origin_ref -> dest_ref,
    most-run first (trip_share desc, then lowest pattern_id, so the order is
    total). [] when the line is not in the timetable or no pattern matches the
    endpoints. Each: {pattern_id, direction, trip_share, is_circular, stops}."""
    key = (operator, line, origin_ref, dest_ref)
    if not all(key):
        return []
    hit = _cands.get(key)
    if hit is not None:                 # the poll's hot path: no re-entry, no stat
        return hit
    prime_routes([key])
    return _cands.get(key, [])


# ---------------------------------------------------------------------------
# Trip pinning (ROADMAP.md 1.13.0 / ADR-0008 Phase 2)
# ---------------------------------------------------------------------------
#
# candidates() above pins a bus to a ROUTE PATTERN (operator, line, endpoints).
# This pins it to one specific scheduled TRIP, by join variant A/C from
# ADR-0008: operator + line + origin stop + exact aimed departure time,
# service actually running that day. ADR-0008 measured this unambiguous
# exact-match rate at ~49% of the non-TfL live fleet and found widening the
# time tolerance bought more matches at the cost of picking between several
# candidates -- so this is exact-match only, by design, not a bug to fix
# later. A miss (no match, or more than one) returns None and the caller
# falls through to the regression exactly as before -- purely additive.

_GTFS_SECS_MACRO = (
    "CREATE OR REPLACE TEMP MACRO gtfs_secs(t) AS "
    "CASE WHEN nullif(trim(t), '') IS NULL THEN NULL "
    "ELSE try_cast(split_part(t, ':', 1) AS INTEGER) * 3600 "
    "+ try_cast(split_part(t, ':', 2) AS INTEGER) * 60 "
    "+ try_cast(split_part(t, ':', 3) AS INTEGER) END"
)


def _local_date_and_secs(epoch_ms):
    """(YYYYMMDD, seconds-since-local-midnight) for an epoch-millis timestamp,
    in Europe/London -- the same zone the dbt side and app.py's hour_band /
    day_type use. None on a bad value rather than raising: a malformed
    aimed-departure field must just fail to pin, not break the poll."""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=_LOCAL_TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return dt.strftime('%Y%m%d'), dt.hour * 3600 + dt.minute * 60 + dt.second


def _load_pins(keys):
    """Fill _pins for many (operator, line, origin_ref, aimed_dep_ms) keys in
    one connection. Two interpretations per key are tried and unioned before
    counting distinct trips, because GTFS times run past 24:00:00 for a
    service that started the previous calendar day: 'today, plain
    seconds-of-day' and 'yesterday, seconds-of-day + 86400' (a trip departing
    00:10 today may be filed as 24:10:00 under yesterday's service_id). A key
    is pinned only if the union of both interpretations resolves to exactly
    one trip_id -- if either interpretation alone is ambiguous but the other
    is empty, or both agree on the same trip, it still counts as one; if they
    disagree, that is a genuine ambiguity and the key is left unpinned."""
    if not available():
        return
    try:
        con = duckdb.connect(GTFS_PATH, read_only=True)
    except Exception:
        return
    found = {k: None for k in keys}
    try:
        con.execute(_GTFS_SECS_MACRO)
        rows = []
        for ix, (op, ln, origin, dep_ms) in enumerate(keys):
            for date_str, secs in _pin_interpretations(dep_ms):
                rows.append(f'{ix}{chr(31)}{_clean(op)}{chr(31)}{_clean(ln)}'
                            f'{chr(31)}{_clean(origin)}{chr(31)}{date_str}{chr(31)}{secs}')
        if not rows:
            return
        con.execute('CREATE TEMP TABLE k(ix INTEGER, op VARCHAR, ln VARCHAR, '
                    'o VARCHAR, d VARCHAR, secs INTEGER)')
        con.execute(
            'INSERT INTO k SELECT split_part(t, chr(31), 1)::INTEGER, '
            'split_part(t, chr(31), 2), split_part(t, chr(31), 3), '
            'split_part(t, chr(31), 4), split_part(t, chr(31), 5), '
            'split_part(t, chr(31), 6)::INTEGER '
            'FROM (SELECT unnest(string_split(?, chr(30))) AS t)', [_pack(rows)])
        con.execute(
            'CREATE TEMP TABLE svc AS '
            'WITH dates AS (SELECT DISTINCT d FROM k), '
            'base AS (SELECT dt.d, cast(cal.service_id AS VARCHAR) AS service_id '
            'FROM dates dt JOIN gtfs_calendar cal ON '
            "(CASE dayofweek(strptime(dt.d, '%Y%m%d')) "
            'WHEN 0 THEN cal.sunday WHEN 1 THEN cal.monday WHEN 2 THEN cal.tuesday '
            'WHEN 3 THEN cal.wednesday WHEN 4 THEN cal.thursday WHEN 5 THEN cal.friday '
            'ELSE cal.saturday END) = 1 '
            "AND coalesce(try_strptime(cast(cal.start_date AS VARCHAR), '%Y%m%d'), "
            "try_cast(cast(cal.start_date AS VARCHAR) AS TIMESTAMP)) <= strptime(dt.d, '%Y%m%d') "
            "AND coalesce(try_strptime(cast(cal.end_date AS VARCHAR), '%Y%m%d'), "
            "try_cast(cast(cal.end_date AS VARCHAR) AS TIMESTAMP)) >= strptime(dt.d, '%Y%m%d')), "
            'removed AS (SELECT dt.d, cast(cd.service_id AS VARCHAR) AS service_id '
            'FROM dates dt JOIN gtfs_calendar_dates cd ON cast(cd.date AS VARCHAR) = dt.d '
            "AND cast(cd.exception_type AS VARCHAR) = '2'), "
            'added AS (SELECT dt.d, cast(cd.service_id AS VARCHAR) AS service_id '
            'FROM dates dt JOIN gtfs_calendar_dates cd ON cast(cd.date AS VARCHAR) = dt.d '
            "AND cast(cd.exception_type AS VARCHAR) = '1') "
            'SELECT d, service_id FROM base EXCEPT SELECT d, service_id FROM removed '
            'UNION SELECT d, service_id FROM added')
        con.execute(
            'CREATE TEMP TABLE noc AS SELECT cast(agency_id AS VARCHAR) AS agency_id, '
            'min(agency_noc) AS noc FROM gtfs_agency GROUP BY 1')
        winners = con.execute(
            'SELECT ix, any_value(trip_id) AS trip_id, any_value(secs) AS secs FROM ('
            'SELECT DISTINCT k.ix, st.trip_id, k.secs '
            'FROM k '
            'JOIN svc ON svc.d = k.d '
            'JOIN gtfs_trips t ON cast(t.service_id AS VARCHAR) = svc.service_id '
            'JOIN gtfs_routes r ON cast(r.route_id AS VARCHAR) = cast(t.route_id AS VARCHAR) '
            'JOIN noc ON noc.agency_id = cast(r.agency_id AS VARCHAR) '
            'JOIN gtfs_stop_times st ON st.trip_id = t.trip_id AND st.stop_id = k.o '
            'AND gtfs_secs(st.departure_time) = k.secs '
            'WHERE noc.noc = k.op AND r.route_short_name = k.ln'
            ') cand '
            'GROUP BY ix HAVING count(DISTINCT trip_id) = 1'
        ).fetchall()
        trip_ids = sorted({tid for _ix, tid, _secs in winners})
        stops = {}
        if trip_ids:
            for tid, seq, atco, arr, dep in con.execute(
                    'SELECT trip_id, stop_sequence, stop_id, gtfs_secs(arrival_time), '
                    'gtfs_secs(departure_time) FROM gtfs_stop_times '
                    'WHERE trip_id IN (SELECT unnest(?)) ORDER BY trip_id, stop_sequence',
                    [trip_ids]).fetchall():
                stops.setdefault(tid, []).append(
                    {'atco': atco, 'seq': seq, 'arr_secs': arr, 'dep_secs': dep})
    except Exception:
        return                          # leave the keys uncached: retried next poll
    finally:
        con.close()
    for ix, tid, matched_secs in winners:
        # anchor_epoch_s + a stop's own arr_secs/dep_secs (raw GTFS
        # seconds-of-service-day, which run past 86400 for a trip that
        # started the previous calendar day) is that stop's real wall-clock
        # epoch. Derived from the key's own aimed-departure instant minus
        # whichever interpretation matched, so it is correct whether the
        # trip's service day was today or yesterday -- see _load_pins.
        _op, _ln, _origin, dep_ms = keys[ix]
        anchor = dep_ms / 1000.0 - matched_secs
        found[keys[ix]] = {'trip_id': tid, 'anchor_epoch_s': anchor,
                            'stops': stops.get(tid, [])}
    _pins.update(found)


def _pin_interpretations(dep_ms):
    """(date, secs) pairs to try for one aimed-departure timestamp: today's
    calendar date at plain seconds-of-day, and yesterday's at +86400, per the
    GTFS past-midnight convention -- see _load_pins."""
    parsed = _local_date_and_secs(dep_ms)
    if parsed is None:
        return []
    date_str, secs = parsed
    try:
        today = datetime.strptime(date_str, '%Y%m%d')
    except ValueError:
        return []
    yesterday = (today - timedelta(days=1)).strftime('%Y%m%d')
    return [(date_str, secs), (yesterday, secs + 86400)]


def prime_pins(keys):
    """Batch-load trip pins for (operator, line, origin_ref, aimed_dep_ms)
    keys; keys already cached, or missing a field, are skipped."""
    _check()
    todo = sorted({key for key in keys if all(key) and key not in _pins})
    if todo:
        _load_pins(todo)


def pin_trip(operator, line, origin_ref, aimed_dep_ms):
    """{'trip_id', 'anchor_epoch_s', 'stops': [{'atco','seq','arr_secs',
    'dep_secs'}, ...]} for the one GTFS trip this vehicle's current journey
    unambiguously matches by (operator, line, origin stop, exact aimed
    departure) -- or None if it does not pin (no match, more than one match,
    or a missing field). See the module note above for why exact-match only.
    anchor_epoch_s + any stop's arr_secs/dep_secs is that stop's real
    wall-clock epoch (see _load_pins for why a plain "local midnight" does
    not always work). stops is the WHOLE trip, ordered by stop_sequence."""
    key = (operator, line, origin_ref, aimed_dep_ms)
    if not all(key):
        return None
    hit = _pins.get(key)
    if key in _pins:
        return hit
    prime_pins([key])
    return _pins.get(key)


def _pattern_stops(operator, line, direction, pattern_id):
    rows = _query(
        'SELECT atco_code, stop_name, locality, lat, lon, seq, sched_secs_from_origin '
        'FROM gtfs_route_stops WHERE operator = ? AND line = ? AND direction = ? '
        'AND pattern_id = ? ORDER BY seq', [operator, line, direction, pattern_id])
    return [_stop_dict(*r[:5], seq=r[5], secs=r[6]) for r in rows]


def _route_dict(stops, operator, r_dir, direction):
    return {'stops': stops, 'operator': operator, 'direction': r_dir,
            'source': 'exact' if not direction or r_dir == direction else 'opposite_direction'}


def _load_lines(keys):
    """Fill _lines for many (operator, line, direction) keys, operator present,
    in one connection. A cold poll has over a thousand distinct keys and a
    connection each took ~80 s. Same resolution as _load_candidates; the pattern
    chosen is the primary one in the requested direction if there is one, else
    outbound, else the one with most trips."""
    if not available():
        return
    try:
        con = duckdb.connect(GTFS_PATH, read_only=True)
    except Exception:
        return
    found = {k: None for k in keys}
    try:
        con.execute('CREATE TEMP TABLE k(op VARCHAR, ln VARCHAR, d VARCHAR)')
        con.execute(
            'INSERT INTO k SELECT split_part(t, chr(31), 1), split_part(t, chr(31), 2), '
            'split_part(t, chr(31), 3) FROM (SELECT unnest(string_split(?, chr(30))) AS t)',
            [_pack(chr(31).join(_clean(f or '') for f in k) for k in keys)])
        con.execute(
            'CREATE TEMP TABLE kr AS SELECT k.*, '
            'CASE WHEN e.operator IS NOT NULL THEN k.op ELSE a.gtfs_operator END AS rop, '
            'CASE WHEN e.operator IS NOT NULL THEN k.ln ELSE a.gtfs_line END AS rln '
            'FROM k LEFT JOIN (SELECT DISTINCT operator, line FROM gtfs_route_patterns) e '
            'ON e.operator = k.op AND e.line = k.ln '
            'LEFT JOIN gtfs_line_alias a ON a.live_operator = k.op AND a.live_line = k.ln')
        con.execute(
            'CREATE TEMP TABLE pick AS SELECT * FROM (SELECT kr.op, kr.ln, kr.d, '
            'p.operator AS pop, p.line AS pline, p.direction AS pdir, p.pattern_id AS pid, '
            'row_number() OVER (PARTITION BY kr.op, kr.ln, kr.d ORDER BY '
            '(p.direction = kr.d) DESC, (p.direction = \'outbound\') DESC, '
            'p.n_trips DESC, p.operator, p.pattern_id) AS rn '
            'FROM kr JOIN gtfs_route_patterns p ON p.operator = kr.rop AND p.line = kr.rln '
            'AND p.is_primary) WHERE rn = 1')
        stops = {}
        for r in con.execute(
                'SELECT pick.op, pick.ln, pick.d, pick.pop, pick.pdir, s.atco_code, '
                's.stop_name, s.locality, s.lat, s.lon, s.seq, s.sched_secs_from_origin '
                'FROM pick JOIN gtfs_route_stops s ON s.operator = pick.pop '
                'AND s.line = pick.pline AND s.direction = pick.pdir '
                'AND s.pattern_id = pick.pid ORDER BY s.seq').fetchall():
            stops.setdefault(r[:5], []).append(
                _stop_dict(r[5], r[6], r[7], r[8], r[9], seq=r[10], secs=r[11]))
    except Exception:
        return                          # leave the keys uncached: retried next poll
    finally:
        con.close()
    for (op, ln, d, pop, pdir), rows in stops.items():
        found[(op, ln, d or None)] = _route_dict(rows, pop, pdir, d or None)
    _lines.update(found)


def prime_lines(keys):
    """Batch-load line routes for (operator, line, direction) keys; keys already
    cached, or without an operator or line, are skipped."""
    _check()
    todo = sorted({k for k in keys if k[0] and k[1] and k not in _lines},
                  key=lambda k: tuple(f or '' for f in k))
    if todo:
        _load_lines(todo)


def line_route(operator, line, direction=None):
    """One representative route for a line, for the map's route overlay:
    {stops, operator, direction, source}, or None. Chooses among the primary
    patterns: the requested direction if it exists, else outbound, else any;
    with no operator, the one carrying the most trips."""
    if not line:
        return None
    _check()
    key = (operator, line, direction)
    if key not in _lines:
        if operator:
            prime_lines([key])
        else:
            rows = _query(
                'SELECT operator, direction, pattern_id FROM gtfs_route_patterns '
                'WHERE line = ? AND is_primary ORDER BY (direction = ?) DESC, '
                '(direction = \'outbound\') DESC, n_trips DESC, operator, pattern_id LIMIT 1',
                [line, direction or ''])
            stops = _pattern_stops(rows[0][0], line, rows[0][1], rows[0][2]) if rows else []
            _lines[key] = (_route_dict(stops, rows[0][0], rows[0][1], direction)
                           if stops else None)
    return _lines.get(key)


def line_localities(norms):
    """{locality_norm: {line, ...}} for the search box. Empty for TfL."""
    norms = sorted({n for n in norms if n})
    if not norms:
        return {}
    out = {}
    for line, norm in _query('SELECT line, locality_norm FROM gtfs_line_localities '
                             'WHERE locality_norm IN (SELECT unnest(?))', [norms]):
        out.setdefault(norm, set()).add(line)
    return out
