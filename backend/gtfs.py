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

try:  # optional at import time, same as app.py
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

GTFS_PATH = os.getenv('GTFS_PATH', '/gtfs/gtfs.duckdb')
GTFS_TTL = 60                # only stat the file once a minute

# app.py sets this to its display_name so stop names read the same as before.
name_fn = lambda s: s  # noqa: E731

_lock = threading.Lock()
_state = {'mtime': None, 'checked': 0.0}
_stops = {}                  # atco -> stop dict, or None for a known miss
_cands = {}                  # (operator, line, origin, dest) -> [pattern dicts]
_lines = {}                  # (operator, line, direction) -> route dict | None
generation = 0               # bumps when the file changes; pattern ids die with it


def _clear():
    global generation
    generation += 1
    _stops.clear()
    _cands.clear()
    _lines.clear()


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
    prime_routes([key])
    return _cands.get(key, [])


def _resolve(operator, line):
    """Like the batch resolution, for one (operator, line): (None, None) if the
    timetable knows neither the line nor an alias for it."""
    if _query('SELECT 1 FROM gtfs_route_patterns WHERE operator = ? AND line = ? LIMIT 1',
              [operator, line]):
        return operator, line
    rows = _query('SELECT gtfs_operator, gtfs_line FROM gtfs_line_alias '
                  'WHERE live_operator = ? AND live_line = ? LIMIT 1', [operator, line])
    return rows[0] if rows else (None, None)


def _pattern_stops(operator, line, direction, pattern_id):
    rows = _query(
        'SELECT atco_code, stop_name, locality, lat, lon, seq, sched_secs_from_origin '
        'FROM gtfs_route_stops WHERE operator = ? AND line = ? AND direction = ? '
        'AND pattern_id = ? ORDER BY seq', [operator, line, direction, pattern_id])
    return [_stop_dict(*r[:5], seq=r[5], secs=r[6]) for r in rows]


def line_route(operator, line, direction=None):
    """One representative route for a line, for the map's route overlay:
    {stops, operator, direction, source}, or None. Chooses among the primary
    patterns: the requested direction if it exists, else outbound, else any;
    with no operator, the one carrying the most trips."""
    if not line:
        return None
    _check()
    key = (operator, line, direction)
    if key in _lines:
        return _lines[key]
    op, ln = _resolve(operator, line) if operator else (None, line)
    if operator and not op:
        found = None
    else:
        where, args = 'line = ? AND is_primary', [ln]
        if op:
            where += ' AND operator = ?'
            args.append(op)
        rows = _query(
            f'SELECT operator, direction, pattern_id FROM gtfs_route_patterns WHERE {where} '
            'ORDER BY (direction = ?) DESC, (direction = \'outbound\') DESC, '
            'n_trips DESC, operator, pattern_id LIMIT 1', args + [direction or ''])
        found = None
        if rows:
            r_op, r_dir, pid = rows[0]
            stops = _pattern_stops(r_op, ln, r_dir, pid)
            if stops:
                found = {'stops': stops, 'operator': r_op, 'direction': r_dir,
                         'source': ('exact' if not direction or r_dir == direction
                                    else 'opposite_direction')}
    _lines[key] = found
    return found


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
