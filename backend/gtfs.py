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


def _clear():
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
        'WHERE ATCOCode IN (SELECT unnest(?)) ORDER BY (Status = \'active\')', [want])
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


def _resolve(operator, line):
    """(operator, line) as the timetable spells it: exact if the timetable has
    it, else through the reviewed alias table. The alias never shadows a real
    match (build_alias.py guarantees it; checking exact first keeps it so)."""
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


def candidates(operator, line, origin_ref, dest_ref):
    """Every timetable pattern of this line that runs origin_ref -> dest_ref,
    most-run first (trip_share desc, then lowest pattern_id, so the order is
    total). [] when the line is not in the timetable or no pattern matches the
    endpoints. Each: {pattern_id, direction, trip_share, is_circular, stops}."""
    if not (operator and line and origin_ref and dest_ref):
        return []
    _check()
    key = (operator, line, origin_ref, dest_ref)
    if key in _cands:
        return _cands[key]
    op, ln = _resolve(operator, line)
    out = []
    if op:
        rows = _query(
            'SELECT direction, pattern_id, trip_share, is_circular FROM gtfs_route_patterns '
            'WHERE operator = ? AND line = ? AND first_atco = ? AND last_atco = ? '
            'ORDER BY trip_share DESC, pattern_id', [op, ln, origin_ref, dest_ref])
        for direction, pattern_id, share, circ in rows:
            out.append({'pattern_id': pattern_id, 'direction': direction,
                        'trip_share': share, 'is_circular': bool(circ),
                        'stops': _pattern_stops(op, ln, direction, pattern_id)})
    _cands[key] = out
    return out


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
