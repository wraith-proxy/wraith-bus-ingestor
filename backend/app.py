"""wraith-bus-ingestor backend: the live bus API.

Serves GeoJSON straight off QuestDB and enriches it with ETA regressions read
out of the dbt marts (a DuckDB file built by a separate job). The marts are
optional by design: if the file is missing, half-written or a table is absent
the API still answers, with status "unknown" and null predictions, rather
than 500ing.
"""

import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime

try:  # stdlib, but the zone DATA is a separate package on slim images
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from flask import Flask, jsonify, request
from flask_cors import CORS

try:  # duckdb is optional at import time so a missing driver degrades, not crashes
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

app = Flask(__name__)
CORS(app)

QUESTDB_URL = os.getenv('QUESTDB_URL', '')
QUESTDB_TIMEOUT = 8          # seconds; keeps any request under the ~10s budget
MARTS_PATH = os.getenv('MARTS_PATH', '/models/wraith_bus_ingestor.duckdb')
MARTS_TTL = 60               # only stat the marts file once a minute

# --------------------------------------------------------------------------
# RAG thresholds — single source of truth for the API and the map colours.
#
# UK PTI (Public Transport Information) punctuality convention: a bus counts as
# on time from 1 minute early through 5 minutes late. Outside that window it is
# early or late.  delay_s = predicted_arrival - scheduled_arrival (+ve = late).
# --------------------------------------------------------------------------
LATE_THRESHOLD_S = 300       # delay_s > 300  -> 'late'
EARLY_THRESHOLD_S = -60      # delay_s < -60  -> 'early'

# Sanity rails on the regression. A bus matched to the wrong destination
# produces an absurd remaining distance; better to admit ignorance than to
# publish a three-hour delay that is really a data-quality bug.
MAX_REMAINING_KM = 150.0
MAX_REMAINING_S = 4 * 3600

# The feed republishes a vehicle's last known journey until it logs on to a new
# one, so a bus sitting overnight still advertises yesterday's arrival time.
# Measured on the live feed: ~25% of vehicles carry a schedule this stale, and
# left alone they dominate the late count with 12-24 hour "delays".
# Everything is stored in UTC, but the UK runs Europe/London, which is UTC+1
# from late March to late October. hour_band and day_type MUST be derived from
# LOCAL time or they disagree with the marts: during BST an hour bucketed on UTC
# is an hour early, and a Saturday 00:30 local ping files under Friday. The dbt
# side uses `AT TIME ZONE 'Europe/London'`; the two must match exactly or every
# lookup silently drops to a coarser grain.
LOCAL_TZ_NAME = 'Europe/London'

MAX_SCHEDULE_AGE_S = 3 * 3600      # aimed arrival this far in the past -> untrusted
MAX_SCHEDULE_AHEAD_S = 12 * 3600   # ...or this far in the future

# The same republishing keeps DEAD VEHICLES in the feed: the record arrives
# fresh, but its SIRI RecordedAtTime -- the moment the position was actually
# measured -- can be hours old, so the bus is frozen on the map while the clock
# runs on and it reads as ever more late. Measured live: median lag 23 s, but
# 32% of vehicles are over 5 minutes stale and 29% over 30 minutes, out to a
# full 24 hours. Those are not late buses, they are yesterday's buses.
MAX_POSITION_AGE_S = 600

# eta_models is fitted on distance travelled ALONG the route, but all this
# service knows at request time is the straight line to the destination stop.
# Median path_km/straight_km over 356k historical journeys is 1.304, so feed the
# model the quantity it was trained on rather than a systematic 30% undercount.
# ponytail: one global constant; make it per-line if rural routes start skewing.
ROUTE_DETOUR_FACTOR = 1.30

# Progress-based delay: how close to a known stop a bus must be for that stop's
# observed time-from-origin to stand in for "where should it be by now".
STOP_MATCH_KM = 0.25
MIN_SCHEDULED_JOURNEY_S = 120   # shorter than this and the fraction is meaningless
MIN_ROUTE_SPAN_S = 120          # origin and destination must be distinguishable
MIN_ROUTE_KM = 1.0              # ...and far enough apart to measure progress along
MIN_TERMINUS_SEPARATION_KM = 1.0  # closer than this and the route is a loop
# A vehicle that has reached its destination and parked there keeps accruing
# delay, because elapsed time is measured against `now` while the journey is
# over: a bus that arrived and sat on the stand reads later every minute. From a
# single snapshot we cannot tell WHEN it arrived, so we cannot honestly say how
# late it was -- only that it is no longer running. Report nothing rather than a
# number that grows on its own.
# Known ceiling: /api/bus/<id> could recover the true arrival from QuestDB
# history (the first ping inside ARRIVED_KM of the destination) and rate it
# properly. That is the upgrade path. /api/buses has no history to work from.
ARRIVED_KM = 0.25          # this close to the destination = arrived, not late
ARRIVED_FRACTION = 0.98    # ...or this far through the journey by progress

# Beyond some delay a genuinely very late bus and a stale journey record are
# indistinguishable from a single snapshot, and a confident "88 minutes late" is
# worse than admitting we do not know. Traced on live data: line 391 / WX75_OBR
# read 88 minutes late on a 92 minute working, but its QuestDB track (53.350 ->
# 53.402 north, then 53.378 -> 53.340 south) shows it finished that working and
# is most of the way through a LATER one, while the feed still publishes the old
# journey's aimed times against it. It was only 108 minutes past its aimed
# arrival, so MAX_SCHEDULE_AGE_S did not catch it, and a bus part-way through its
# next trip always looks like it made progress, so an "a whole journey late"
# rule does not catch it either.
# KNOWN CEILING: this rejects the tail, it does not identify it. The real fix is
# to compare consecutive journey_ref values from QuestDB history -- a vehicle
# that has rolled onto a new working changes journey_ref, which says outright
# what this threshold can only guess. See the sizing in the round-three report.
# Swept against 2193 rated live vehicles across fraction 0.4/0.5/0.6 x floor
# 600/900 s. Every one of the six settings produced the SAME maximum surviving
# delay (2083 s, 34.7 min) and removed the same implausible cases -- the 92
# minute workings reading 74 and 104 minutes late, the 35 minute working reading
# 59. So the visible tail does not discriminate between them and the only real
# difference is collateral: how many honest, still-running late buses get
# censored. 0.6 censors the fewest (10 of 2193, 0.46%, against 29 at 0.4/600).
# The floor matters for short workings: at 900 s a 15 minute route is allowed to
# be a full 15 minutes late before we disbelieve it, where a 600 s floor would
# start refusing at 10 -- and short routes are exactly where a real delay is
# large relative to the journey. Net effect of the pair: a long working is
# capped at 0.6 of its own duration, a short one at roughly 1.0.
STALE_JOURNEY_FRACTION = 0.6
STALE_JOURNEY_FLOOR_S = 900

MIN_STOP_HOP_S = 30             # adjacent stops often share a median time
GRID = 2  # decimal places for the stop lookup grid (~1.1 km cells)

# QuestDB is queried as a URL-encoded SQL string, so anything interpolated into
# a query is an injection vector. Whitelist and reject; never escape-and-hope.
BUS_ID_RE = re.compile(r'^[A-Za-z0-9_:.\-]{1,64}$')
LINE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._:\-]{0,31}$')
DIRECTION_RE = re.compile(r'^[A-Za-z_]{1,16}$')
OPERATOR_RE = re.compile(r'^[A-Za-z0-9_]{1,16}$')
DAY_TYPES = ('weekday', 'sat', 'sun', '*')

# A service number is not a route. '88' is several unrelated routes in different
# towns that happen to share a number, and line_stops keys on the number
# alone, so a low-numbered line's merged stop set can zig-zag between counties.
# Cap what we will draw either way -- riders only board at a stop, never on
# the road between two, so a plausible road-snapped guess through even a
# merged stop list is fine; MERGED_ROUTE_HOP_KM still splits it into
# disconnected segments at the implausible jumps so one bad number does not
# draw a single line across half the country, and geometry_quality still
# tells the map this stop sequence itself is suspect.
MAX_ROUTE_STOPS = 400
MERGED_ROUTE_HOP_KM = 15.0   # consecutive stops further apart than this are suspect

# Self-hosted OSRM (car profile -- there is no bus profile, and a bus mostly
# drives the same streets a car does), used to snap the /api/route LineString
# to real roads instead of straight lines between stops. Anything can make
# this call fail -- OSRM down, a stop pair with no drivable path, a slow
# graph query -- and none of it should ever turn into a broken map, so every
# caller of road_snapped_geometry() must treat None as "draw the straight
# line instead", never as an error.
OSRM_URL = os.getenv('OSRM_URL', 'http://osrm:5000')
OSRM_TIMEOUT_S = 5.0

EARTH_RADIUS_KM = 6371.0088

OPPOSITE = {'inbound': 'outbound', 'outbound': 'inbound'}
STOP_RADIUS_KM = 0.100       # "near a stop" = within 100 m
HISTORY_MINUTES = 90
FRESHNESS_MINUTES = 5

_logged = set()


def log_once(key, message):
    """Complain about a broken dependency once, not once per request."""
    if key not in _logged:
        _logged.add(key)
        print(f'[bus] {message}', flush=True)


# --------------------------------------------------------------------------
# Pure helpers (covered by test_bus_api.py)
# --------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def classify_status(delay_s):
    """RAG bucket for a delay in seconds. See the threshold block above."""
    if delay_s is None:
        return 'unknown'
    if delay_s > LATE_THRESHOLD_S:
        return 'late'
    if delay_s < EARLY_THRESHOLD_S:
        return 'early'
    return 'on_time'


def normalise_query(q):
    """Match localities.locality_norm: upper, punctuation stripped,
    whitespace collapsed, trimmed."""
    if not q:
        return ''
    s = re.sub(r'[^A-Z0-9 ]+', ' ', str(q).upper())
    return re.sub(r'\s+', ' ', s).strip()


_local_tz = {'zone': None, 'resolved': False}


def local_zone():
    """Europe/London, or UTC with one logged warning if the zone data is absent.

    zoneinfo is stdlib but python:3.11-slim carries no zone database, so the
    image pip-installs `tzdata`. If that ever goes missing we must degrade, not
    crash: on UTC every band is an hour out during BST and the grain quietly
    coarsens, which is survivable; a stack trace per request is not."""
    if not _local_tz['resolved']:
        _local_tz['resolved'] = True
        if ZoneInfo is None:
            log_once('tz', 'zoneinfo unavailable; falling back to UTC -- hour_band '
                           'and day_type will be an hour out during BST')
        else:
            try:
                _local_tz['zone'] = ZoneInfo(LOCAL_TZ_NAME)
            except Exception as exc:
                log_once('tz', f'cannot load {LOCAL_TZ_NAME} ({exc}); falling back to UTC -- '
                         'hour_band '
                               'and day_type will be an hour out during BST')
        if _local_tz['zone'] is None:
            _local_tz['zone'] = UTC
    return _local_tz['zone']


def local_dt(epoch_s):
    """Epoch seconds as a Europe/London datetime. Every temporal grain -- both
    hour_band and day_type -- is derived from this and never from UTC."""
    return datetime.fromtimestamp(float(epoch_s), tz=local_zone())


def day_type(dt):
    """'weekday' | 'sat' | 'sun' from a LOCAL datetime.

    Saturday and Sunday operation differs from Mon-Fri in both frequency and
    running time, so they are fitted separately. Bank holidays run a Sunday-ish
    service and are deliberately NOT handled -- they file as 'weekday'. Building
    a holiday calendar is out of scope; this is the documented gap."""
    weekday = dt.weekday()
    if weekday == 5:
        return 'sat'
    if weekday == 6:
        return 'sun'
    return 'weekday'


def hour_band(dt):
    """Bucket a LOCAL hour into the bands the dbt model groups on. The marts
    carry '*' fallback rows, so a disagreement over the boundaries degrades to a
    coarser model rather than to no model at all."""
    h = dt.hour
    if h < 6 or h >= 22:
        return 'night'
    if h < 10:
        return 'am_peak'
    if h < 16:
        return 'inter'
    if h < 19:
        return 'pm_peak'
    return 'evening'


def pick_model(models, operator, line, direction, day, band):
    """Best-matching eta_models row, most specific grain first.

    Grain is (operator, line, direction, day_type, hour_band); each fallback
    row is refitted at its own grain rather than averaged.

    Operator, direction and day are all allowed to miss. operator is NULL on
    every ping before 2026-09-12, so the operator-specific rungs are skipped
    entirely (not merely tried and missed) whenever the live bus has none --
    trying them anyway would only ever hit a '*'-operator row, uselessly
    labelled as if it were operator-specific. The marts' 'inbound'/'outbound'
    is inferred from journey geometry over history that predates the schedule
    columns, so it need not agree with the live feed; and while the marts are
    being rebuilt they may carry no day_type at all, in which case every row
    is filed under '*'. Any mismatch must fall through to a coarser grain
    rather than yield no prediction."""
    rungs = []
    if operator:
        rungs.extend([
            ((operator, line, direction, day, band), 'operator+line+direction+day+hour_band'),
            ((operator, line, direction, day, '*'), 'operator+line+direction+day'),
            ((operator, line, direction, '*', band), 'operator+line+direction+hour_band'),
            ((operator, line, direction, '*', '*'), 'operator+line+direction'),
            ((operator, line, '*', '*', '*'), 'operator+line'),
        ])
    rungs.extend([
        (('*', line, direction, day, band), 'line+direction+day+hour_band'),
        (('*', line, direction, day, '*'), 'line+direction+day'),
        (('*', line, direction, '*', band), 'line+direction+hour_band'),
        (('*', line, direction, '*', '*'), 'line+direction'),
        (('*', line, '*', '*', '*'), 'line'),
        (('*', '*', '*', '*', '*'), 'global'),
    ])
    for key, source in rungs:
        row = models.get(key)
        if row:
            out = dict(row)
            out['source'] = source
            return out
    return None


def display_name(value):
    """The feed hands us names with underscores for spaces
    (`Stockport_Interchange`). Only swap them back when the string has no real
    spaces, so a genuinely underscored name survives."""
    if not value:
        return None
    value = str(value)
    if '_' in value and ' ' not in value:
        value = value.replace('_', ' ')
    return value.strip() or None


def iso_from_epoch_s(seconds):
    if seconds is None:
        return None
    return datetime.fromtimestamp(float(seconds), tz=UTC).isoformat().replace('+00:00', 'Z')


def iso_from_millis(millis):
    if millis is None:
        return None
    try:
        return iso_from_epoch_s(float(millis) / 1000.0)
    except (TypeError, ValueError):
        return None


def epoch_from_qdb_timestamp(value):
    """QuestDB hands back '2026-09-12T09:03:00.000000Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Marts (DuckDB, read-only, cached in memory)
# --------------------------------------------------------------------------

# How the last load of the marts went. 'locked' and 'missing' must not look
# alike to anyone reading /api/model-info: a locked file is a rebuild in
# progress and will fix itself, a missing one will not.
MARTS_MISSING = 'missing'
MARTS_LOCKED = 'locked'
MARTS_UNREADABLE = 'unreadable'
MARTS_NO_DRIVER = 'no_driver'
MARTS_OK = 'ok'

EMPTY_MARTS = {
    'ok': False, 'built_at': None, 'n_models': 0, 'day_types': [],
    'state': MARTS_MISSING, 'error': None,
    'reload_state': None, 'reload_error': None,
    'eta': {}, 'localities': [], 'line_loc': {}, 'stops': {}, 'stop_by_atco': {},
    'stop_grid': {}, 'stop_by_line_atco': {}, 'route_geom': {},
}

_marts_lock = threading.Lock()
_marts_state = {'data': None, 'mtime': None, 'checked': 0.0}

# Last real ETA we managed to compute per bus_id, as a fallback of last resort
# once the regression, geometric progress and stop-sequence progress have all
# come up empty. A GIL-protected dict is good enough here: worst case under a
# race is one bus briefly serving last tick's ETA, same as a slightly-stale
# poll would anyway. Unbounded, but bounded in practice by the live fleet size
# (a few thousand bus_ids) and reset on every process restart.
_last_eta = {}


def _table_columns(con, table):
    """Columns a mart actually has. The dbt side is rebuilt independently, so
    for a while wraith_bus_ingestor.duckdb still carries the pre-day_type shape. Detect
    rather than assume: a missing column must degrade the grain, not throw and
    disable every prediction."""
    try:
        rows = con.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_name = ?', [table]).fetchall()
        return {r[0] for r in rows}
    except Exception as exc:
        log_once('marts-cols-' + table, f'cannot introspect {table}: {exc}')
        return set()


def _fetch(con, sql, key):
    try:
        return con.execute(sql).fetchall()
    except Exception as exc:
        log_once('marts-' + key, f'mart {key} unreadable: {exc}')
        return []


def _load_marts(path):
    marts = dict(EMPTY_MARTS)
    marts.update({'eta': {}, 'localities': [], 'line_loc': {}, 'stops': {},
                  'stop_by_atco': {}, 'stop_grid': {},
                  'stop_by_line_atco': {}, 'day_types': [],
                  # Lazily filled by road_snapped_geometry()'s caller, keyed by
                  # (line, direction, day). A fresh dict every reload, so a
                  # rebuilt marts file (new stop sequences) can never serve a
                  # road geometry cached against the old ones.
                  'route_geom': {},
                  'state': MARTS_MISSING, 'error': None,
                  'reload_state': None, 'reload_error': None})

    if duckdb is None:
        marts['state'] = MARTS_NO_DRIVER
        marts['error'] = 'duckdb not installed'
        log_once('marts-nodriver', 'duckdb not installed; predictions disabled')
        return marts
    # A bind mount of a not-yet-existing file makes Docker create a directory.
    if os.path.isdir(path) or not os.path.isfile(path):
        marts['state'] = MARTS_MISSING
        marts['error'] = f'no file at {path}'
        log_once('marts-missing', f'marts not present at {path}; predictions disabled')
        return marts

    try:
        con = duckdb.connect(path, read_only=True)
    except Exception as exc:
        # A writer holding the file is a rebuild in progress: transient, and a
        # completely different thing from the file not being there. Say which.
        text = str(exc)
        locked = 'lock' in text.lower()
        marts['state'] = MARTS_LOCKED if locked else MARTS_UNREADABLE
        marts['error'] = text.split('\n')[0][:200]
        log_once('marts-open-' + marts['state'],
                 'cannot open marts {} ({}): {}'.format(path, marts['state'], text))
        return marts

    try:
        # Old-shape marts have no day_type (or operator) column; select a
        # literal '*' in its place so every row lands on the agnostic rungs
        # of the chain rather than failing to load at all.
        eta_cols = _table_columns(con, 'eta_models')
        day_expr = 'day_type' if 'day_type' in eta_cols else "'*'"
        eta_op_expr = 'operator' if 'operator' in eta_cols else "'*'"
        if day_expr != 'day_type':
            log_once('marts-noday-eta',
                     'eta_models has no day_type column; treating every model '
                     'as day_type "*" until the marts are rebuilt')
        if eta_op_expr != 'operator':
            log_once('marts-noop-eta',
                     'eta_models has no operator column; treating every model '
                     'as operator "*" until the marts are rebuilt')
        for operator, line, direction, day, band, slope, intercept, r2, n_obs in _fetch(
            con,
            f'SELECT {eta_op_expr}, line, direction, {day_expr}, hour_band, '
            'slope_s_per_km, intercept_s, r2, n_obs FROM eta_models', 'eta_model'):
            marts['eta'][(operator, line, direction, day, band)] = {
                'slope_s_per_km': slope, 'intercept_s': intercept,
                'r2': r2, 'n_obs': n_obs,
            }

        for norm, locality, lat, lon, n_stops in _fetch(
            con,
            'SELECT locality_norm, locality, lat, lon, n_stops FROM localities',
            'localities'):
            marts['localities'].append({
                'locality_norm': norm, 'locality': locality,
                'lat': lat, 'lon': lon, 'n_stops': n_stops,
            })

        for line, norm in _fetch(
            con, 'SELECT line, locality_norm FROM line_localities', 'line_localities'):
            marts['line_loc'].setdefault(norm, set()).add(line)

        stop_cols = _table_columns(con, 'line_stops')
        sday_expr = 'day_type' if 'day_type' in stop_cols else "'*'"
        sop_expr = 'operator' if 'operator' in stop_cols else "'*'"
        if sday_expr != 'day_type':
            log_once('marts-noday-stops',
                     'line_stops has no day_type column; treating every stop '
                     'sequence as day_type "*" until the marts are rebuilt')
        if sop_expr != 'operator':
            log_once('marts-noop-stops',
                     'line_stops has no operator column; treating every stop '
                     'sequence as operator "*" until the marts are rebuilt')
        for operator, line, direction, day, atco, name, locality, lat, lon, seq, secs in _fetch(
            con,
            f'SELECT {sop_expr}, line, direction, {sday_expr}, atco_code, stop_name, '
            'locality, lat, lon, seq, median_secs_from_origin FROM line_stops',
            'line_stops'):
            stop = {
                'atco': atco, 'name': display_name(name), 'locality': locality,
                'lat': lat, 'lon': lon, 'seq': seq, 'secs_from_origin': secs,
            }
            marts['stops'].setdefault((operator, line, direction, day), []).append(stop)
            if atco and atco not in marts['stop_by_atco']:
                marts['stop_by_atco'][atco] = stop
    finally:
        con.close()  # never hold a handle across requests

    for stops in marts['stops'].values():
        stops.sort(key=lambda s: (s['seq'] is None, s['seq']))

    # Grid index so matching a bus to its nearest stop is a handful of distance
    # checks rather than a scan of every stop on the line -- /api/buses does this
    # for ~6000 vehicles on every poll.
    for key, stops in marts['stops'].items():
        cells = marts['stop_grid'].setdefault(key, {})
        by_atco = marts['stop_by_line_atco'].setdefault(key, {})
        for stop in stops:
            if stop['atco']:
                by_atco.setdefault(stop['atco'], stop)
            if stop['lat'] is None or stop['lon'] is None:
                continue
            key = (round(stop['lat'], GRID), round(stop['lon'], GRID))
            cells.setdefault(key, []).append(stop)

    marts['ok'] = True
    marts['state'] = MARTS_OK
    marts['n_models'] = len(marts['eta'])
    # Keys are (operator, line, direction, day_type, ...) for eta and
    # (operator, line, direction, day_type) for stops -- day_type is index 3
    # in both since operator joined the front of the key.
    days = {k[3] for k in marts['eta']} | {k[3] for k in marts['stops']}
    marts['day_types'] = sorted(d for d in days if d)
    try:
        marts['built_at'] = iso_from_epoch_s(os.path.getmtime(path))
    except OSError:
        marts['built_at'] = None
    return marts


def get_marts():
    """Cached marts, reloaded when the file's mtime moves. The mtime is checked
    at most once a minute."""
    state = _marts_state
    if state['data'] is not None and time.time() - state['checked'] < MARTS_TTL:
        return state['data']
    with _marts_lock:
        if state['data'] is not None and time.time() - state['checked'] < MARTS_TTL:
            return state['data']
        state['checked'] = time.time()
        try:
            mtime = os.path.getmtime(MARTS_PATH) if os.path.isfile(MARTS_PATH) else None
        except OSError:
            mtime = None
        if state['data'] is None or mtime != state['mtime']:
            fresh = _load_marts(MARTS_PATH)
            if fresh['ok'] or not (state['data'] or {}).get('ok'):
                state['data'] = fresh
                # Only remember the mtime we actually managed to read, so a
                # failed load is retried on the next tick instead of being
                # cached as if it had succeeded.
                state['mtime'] = mtime if fresh['ok'] else None
            else:
                # A rebuild in progress must not blank out a working API. Keep
                # serving the marts we already have and record why the refresh
                # failed, so /api/model-info can say the data is going stale.
                state['data'] = dict(state['data'],
                                     reload_state=fresh['state'],
                                     reload_error=fresh['error'])
                state['mtime'] = None
        return state['data']


# --------------------------------------------------------------------------
# QuestDB
# --------------------------------------------------------------------------

BASE_COLUMNS = ['bus_id', 'line', 'lat', 'lon', 'bearing', 'timestamp']
EXTRA_COLUMNS = [
    'operator', 'direction', 'journey_ref', 'published_line',
    'origin_ref', 'origin_name', 'dest_ref', 'dest_name',
    'origin_aimed_dep', 'dest_aimed_arr', 'recorded_at', 'block_ref',
]

_columns_cache = {'cols': None, 'checked': 0.0}


def qdb_exec(sql):
    url = QUESTDB_URL + '/exec?query=' + urllib.parse.quote(sql)
    with urllib.request.urlopen(url, timeout=QUESTDB_TIMEOUT) as resp:
        return json.load(resp)


def qdb_rows(sql):
    """Run a query and return rows as dicts keyed by column name."""
    payload = qdb_exec(sql)
    names = [c['name'] for c in payload.get('columns', [])]
    return [dict(zip(names, row)) for row in payload.get('dataset', [])]


def available_columns():
    """The enriched columns are created lazily by the ingestor's ILP writes, so
    select only what actually exists or QuestDB 400s the whole query."""
    now = time.time()
    if _columns_cache['cols'] is None or now - _columns_cache['checked'] > MARTS_TTL:
        try:
            payload = qdb_exec('SHOW COLUMNS FROM bus_positions')
            _columns_cache['cols'] = {row[0] for row in payload.get('dataset', [])}
        except Exception as exc:
            log_once('qdb-columns', f'cannot read bus_positions columns: {exc}')
            _columns_cache['cols'] = _columns_cache['cols'] or set(BASE_COLUMNS)
        _columns_cache['checked'] = now
    return _columns_cache['cols']


def select_columns():
    have = available_columns()
    return BASE_COLUMNS + [c for c in EXTRA_COLUMNS if c in have]


def live_buses(bus_id=None):
    cols = ', '.join(select_columns())
    where = f"timestamp > dateadd('m', -{FRESHNESS_MINUTES}, now())"
    if bus_id:
        where = f"bus_id = '{bus_id}' AND {where}"
    return qdb_rows(
        f"SELECT {cols} FROM bus_positions WHERE {where} "
        "LATEST ON timestamp PARTITION BY bus_id")


def bus_history(bus_id):
    return qdb_rows(
        f"SELECT lat, lon, timestamp FROM bus_positions "
        f"WHERE bus_id = '{bus_id}' AND timestamp > dateadd('m', -{HISTORY_MINUTES}, now()) "
        "ORDER BY timestamp DESC LIMIT 500")


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------

def schedule_epoch_s(millis, now_s, max_age_s, max_ahead_s):
    """Epoch seconds for an aimed time, or None if it is too stale to trust.

    The feed keeps republishing a vehicle's last journey until it logs on to the
    next one, so an overnight bus advertises yesterday's timetable. Those records
    are not late buses, they are dead records."""
    if millis is None:
        return None
    try:
        secs = float(millis) / 1000.0
    except (TypeError, ValueError):
        return None
    age = now_s - secs
    if age > max_age_s or age < -max_ahead_s:
        return None
    return secs


def stops_index_key(index, operator, line, direction, day):
    """Resolve an (operator, line, direction, day_type) key against a stops
    index.

    Prefers the bus's own operator, then the '*' row pooled across every
    operator (every pre-2026-09-12 observation can only ever live there); then
    the live day_type, then the '*' row set the marts always emit; and the
    feed's direction, then its opposite (the marts' direction is inferred from
    geometry and may be the other way round for a given line). All three stop
    indexes are built from the same keys, so this resolves identically for
    each and they never mix operator or day sets."""
    for op in (operator, '*'):
        for d in (direction, OPPOSITE.get(direction)):
            for dt in (day, '*'):
                key = (op, line, d, dt)
                if index.get(key):
                    return key
    return None


def nearest_stop(marts, operator, line, direction, day, lat, lon):
    """Closest stop on this line to a position, within STOP_MATCH_KM.

    Checks only the bus's own grid cell and its eight neighbours."""
    if lat is None or lon is None:
        return None, None
    key = stops_index_key(marts['stop_grid'], operator, line, direction, day)
    cells = marts['stop_grid'].get(key) if key else None
    if not cells:
        return None, None
    step = 10 ** -GRID
    best, best_km = None, STOP_MATCH_KM
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            key = (round(lat + dy * step, GRID), round(lon + dx * step, GRID))
            for stop in cells.get(key, ()):
                km = haversine_km(lat, lon, stop['lat'], stop['lon'])
                if km < best_km:
                    best, best_km = stop, km
    return best, (round(best_km, 3) if best else None)


def _secs(stops_by_atco, atco):
    stop = stops_by_atco.get(atco or '')
    if stop is None:
        return None
    val = stop.get('secs_from_origin')
    return None if val is None else float(val)


def progress_delay(row, marts, now_s, operator, line, direction, day, lat, lon):
    """Delay from schedule adherence rather than from the ETA regression.

    The feed gives aimed times for the journey's origin and destination only --
    there are no per-stop scheduled times anywhere in SIRI-VM. So we take the
    SHAPE of the route from our own history (line_stops knows the median
    seconds a journey needs to reach each stop) and stretch it onto THIS
    journey's advertised timetable:

        fraction  = (here - origin) / (destination - origin)   [observed shape]
        expected  = fraction * (aimed_arrival - aimed_departure)
        delay     = (now - aimed_departure) - expected

    Anchoring on the origin stop rather than on the journey's first ping matters:
    secs_from_origin is measured from wherever tracking happened to begin, so
    using it raw hands every bus its own depot-to-terminus lag as permanent
    lateness. Expressed as a fraction those anchors cancel, and at the last stop
    the whole thing collapses to plain actual-arrival vs aimed-arrival.

    Errors in absolute speed cancel too, which is the point: the ETA
    regression's in-sample MAE is 6-11 minutes, wider than the 6-minute on-time
    window, so it cannot carry the RAG classification on its own."""
    departed = schedule_epoch_s(row.get('origin_aimed_dep'), now_s,
                                MAX_SCHEDULE_AGE_S, MAX_SCHEDULE_AHEAD_S)
    aimed_arr = schedule_epoch_s(row.get('dest_aimed_arr'), now_s,
                                 MAX_SCHEDULE_AGE_S, MAX_SCHEDULE_AHEAD_S)
    if departed is None or aimed_arr is None:
        return None, None
    scheduled = aimed_arr - departed
    if scheduled < MIN_SCHEDULED_JOURNEY_S or scheduled > MAX_REMAINING_S:
        return None, None

    stop, _km = nearest_stop(marts, operator, line, direction, day, lat, lon)
    if stop is None:
        return None, None
    here = stop.get('secs_from_origin')
    if here is None:
        return None, None

    akey = stops_index_key(marts['stop_by_line_atco'], operator, line, direction, day)
    by_atco = (marts['stop_by_line_atco'].get(akey) or {}) if akey else {}
    s_origin = _secs(by_atco, row.get('origin_ref'))
    s_dest = _secs(by_atco, row.get('dest_ref'))
    if s_origin is None or s_dest is None:
        return None, None
    span = s_dest - s_origin
    if span < MIN_ROUTE_SPAN_S:
        return None, None          # origin and destination indistinguishable

    fraction = (float(here) - s_origin) / span
    if not -0.1 <= fraction <= 1.1:
        return None, None          # matched a stop that is not on this leg
    return _delay_from_fraction(fraction, departed, scheduled, now_s), stop


def _delay_from_fraction(fraction, departed, scheduled, now_s):
    """Delay implied by how far through the journey the bus is.

    Two refusals, both measured on live data:

    1. At or past the end of the route the journey is over, but `now` keeps
       advancing, so the arithmetic charges the vehicle for every minute it
       sits on the stand. A line 411 working scheduled 09:45-10:30 was parked at
       its destination reading 35 minutes late for exactly this reason (192 of
       2434 rated vehicles). A single snapshot cannot say when it arrived, so it
       cannot say how late it was -- only that it is no longer running.
    2. Past a point, "very late" and "the feed is still publishing a finished
       journey's timetable against this vehicle" are the same picture. The
       ceiling is a fraction of the journey's own scheduled duration, floored so
       that short workings are not held to an implausibly tight bound. See
       STALE_JOURNEY_FRACTION for the evidence and the proper fix."""
    if fraction >= ARRIVED_FRACTION:
        return None                # arrived; see (1)
    fraction = min(1.0, max(0.0, fraction))
    elapsed = now_s - departed
    if elapsed < 0:
        return None                # not started yet; nothing to be late for
    delay = int(round(elapsed - fraction * scheduled))
    if delay > max(STALE_JOURNEY_FLOOR_S, STALE_JOURNEY_FRACTION * scheduled):
        return None                # not credibly this late; see (2)
    return delay


def geometric_progress_delay(row, marts, now_s):
    """Same idea as progress_delay, but measuring progress as plain distance
    covered between the origin and destination stops.

    progress_delay needs the bus to be near a stop the marts already know on
    this line, which holds for only a couple of hundred vehicles. This needs
    nothing but the two terminus coordinates, which the feed gives as ATCO codes
    on almost every vehicle, so it covers most of the fleet. It assumes progress
    is uniform in distance rather than in time -- crude in town centres -- but it
    is unbiased at both ends, and at the destination it collapses to exactly the
    arrival-vs-timetable comparison."""
    departed = schedule_epoch_s(row.get('origin_aimed_dep'), now_s,
                                MAX_SCHEDULE_AGE_S, MAX_SCHEDULE_AHEAD_S)
    aimed_arr = schedule_epoch_s(row.get('dest_aimed_arr'), now_s,
                                 MAX_SCHEDULE_AGE_S, MAX_SCHEDULE_AHEAD_S)
    if departed is None or aimed_arr is None:
        return None
    scheduled = aimed_arr - departed
    if scheduled < MIN_SCHEDULED_JOURNEY_S or scheduled > MAX_REMAINING_S:
        return None

    lat, lon = row.get('lat'), row.get('lon')
    origin = marts['stop_by_atco'].get(row.get('origin_ref') or '')
    dest = marts['stop_by_atco'].get(row.get('dest_ref') or '')
    if not origin or not dest or lat is None or lon is None:
        return None
    if origin.get('lat') is None or dest.get('lat') is None:
        return None

    # Circular routes defeat this method outright. When a journey ends where it
    # began, distance-from-origin and distance-to-destination are the same
    # number all the way round, so the fraction sticks at 0.5 and a bus 90% of
    # the way round its loop reads as half done -- permanently, and increasingly,
    # late. Liverpool ONE, Queen Square and the airport circulars were all
    # reading 25-50 minutes late this way. Refuse rather than guess; these fall
    # through to the stop-sequence method, which knows the order of a loop.
    if haversine_km(origin['lat'], origin['lon'],
                    dest['lat'], dest['lon']) < MIN_TERMINUS_SEPARATION_KM:
        return None

    done = haversine_km(origin['lat'], origin['lon'], lat, lon)
    togo = haversine_km(lat, lon, dest['lat'], dest['lon'])
    total = done + togo
    if total < MIN_ROUTE_KM:
        return None
    # Standing at the destination: the journey is over and the delay would grow
    # for as long as the bus sits there. The fraction test in
    # _delay_from_fraction catches the same case proportionally; this catches it
    # in absolute terms, which matters on a long route where 250 m is a tiny
    # fraction, and on a short one where it is most of the way.
    if togo <= ARRIVED_KM:
        return None
    return _delay_from_fraction(done / total, departed, scheduled, now_s)


def position_is_live(row, now_s):
    """Is this vehicle actually reporting, or is the feed replaying a dead record?

    Freshness has to be judged on the SIRI RecordedAtTime, not on when we
    ingested the row: a stale record is re-served to us every 60 seconds and so
    always looks new. Vehicles with no recorded_at at all (pre-patch rows, and
    operators that omit it) are let through -- the five-minute ingest window is
    the only freshness signal available for those."""
    recorded = row.get('recorded_at')
    if recorded is None:
        return True
    try:
        age = now_s - float(recorded) / 1000.0
    except (TypeError, ValueError):
        return True
    return age <= MAX_POSITION_AGE_S


def predict(row, marts, now_s):
    """Return (properties-fragment, model, predicted_epoch_s)."""
    line = row.get('line')
    direction = row.get('direction')
    operator = row.get('operator')
    lat, lon = row.get('lat'), row.get('lon')

    straight_km = None
    dest = marts['stop_by_atco'].get(row.get('dest_ref') or '')
    if dest and lat is not None and lon is not None and dest.get('lat') is not None:
        straight_km = haversine_km(lat, lon, dest['lat'], dest['lon'])
    # The model was fitted on along-route distance; scale the straight line up.
    remaining_km = round(straight_km * ROUTE_DETOUR_FACTOR, 3) if straight_km is not None else None

    local = local_dt(now_s)          # Europe/London, never UTC -- see local_zone()
    day = day_type(local)
    model = pick_model(marts['eta'], operator, line, direction, day, hour_band(local))

    eta_iso = None
    eta_source = None
    predicted = None
    if remaining_km is not None and model and remaining_km <= MAX_REMAINING_KM:
        secs = max(0.0, (model['slope_s_per_km'] or 0.0) * remaining_km
                   + (model['intercept_s'] or 0.0))
        if secs <= MAX_REMAINING_S:
            predicted = now_s + secs
            eta_iso = iso_from_epoch_s(predicted)
            eta_source = 'regression'

    # Whichever method produces it, a delay is refused outright once the bus is
    # essentially at its destination: the journey is over, so elapsed-versus-
    # schedule just charges it for standing on the stand. The geometric method
    # applies the same rail to its own distance-to-terminus, but the
    # stop-sequence method measures progress in median seconds rather than
    # metres, so it can still be short of ARRIVED_FRACTION while physically
    # parked at the terminus. Catch it here, once, for both.
    arrived = straight_km is not None and straight_km <= ARRIVED_KM

    # Delay comes from schedule adherence only. The regression is deliberately
    # NOT used for it: its intercept is ~750 s because it is fitted to the last
    # ping of a journey, which lands after the terminus dwell rather than at the
    # arrival, and measured against a model-free check at destination stops it
    # reads about 7 minutes late across the board. Fine for a rough ETA on a bus
    # with no timetable; not fine as the thing that colours the map.
    # Distance-based progress first. Validated against a model-free check of
    # buses sitting at their destination stop, it lands within a couple of
    # seconds of the truth (median +98 s vs +96 s observed). The stop-sequence
    # version is in principle better -- it knows where a route actually dawdles
    # -- but line_stops used to key on line number alone, where a number
    # like '88' is several unrelated routes merged, so in practice it read ~6
    # minutes late. operator joined the mart grain 2026-09-12 (see
    # line_stops), which fixes that for a bus whose operator has enough
    # observed history to fit its own sequence; for everything else this
    # still falls back to the same merged, pooled ('*') sequence as before.
    # Keeping this as the fallback for circular routes only, rather than
    # promoting it above the geometric method generally, is a separate
    # accuracy call this fix does not make on its own.
    delay_s = None if arrived else geometric_progress_delay(row, marts, now_s)
    delay_source = 'progress_distance' if delay_s is not None else None
    if delay_s is None and not arrived:
        delay_s, _stop = progress_delay(
            row, marts, now_s, operator, line, direction, day, lat, lon)
        delay_source = 'progress_stops' if delay_s is not None else None

    # With a delay in hand, the timetable plus that delay beats the regression.
    aimed = schedule_epoch_s(row.get('dest_aimed_arr'), now_s,
                             MAX_SCHEDULE_AGE_S, MAX_SCHEDULE_AHEAD_S)
    if delay_s is not None and aimed is not None:
        predicted = aimed + delay_s
        eta_iso = iso_from_epoch_s(predicted)
        eta_source = 'timetable_delay'

    # Every bus should show an ETA wherever we can honestly produce one.
    # Regression, then schedule-adjusted timetable, above, both failed --
    # fall back to the bare timetable (no delay applied, since we have none),
    # then to whatever ETA we last actually computed for this vehicle. Both
    # are worse than a fresh number but better than a blank cell on the map.
    bus_id = row.get('bus_id')
    if eta_iso is None and aimed is not None:
        predicted = aimed
        eta_iso = iso_from_epoch_s(predicted)
        eta_source = 'timetable_raw'
    if eta_iso is None and bus_id is not None:
        cached = _last_eta.get(bus_id)
        if cached is not None:
            predicted, eta_iso = cached
            eta_source = 'last_known'
    elif eta_iso is not None and bus_id is not None:
        _last_eta[bus_id] = (predicted, eta_iso)

    return {
        'remaining_km': remaining_km,
        'eta_iso': eta_iso,
        'eta_source': eta_source,
        'delay_s': delay_s,
        'delay_source': delay_source,
        'status': classify_status(delay_s),
    }, model, predicted


def bus_properties(row, marts, now_s):
    frag, model, predicted = predict(row, marts, now_s)
    props = {
        'bus_id': row.get('bus_id'),
        'line': row.get('line'),
        'bearing': row.get('bearing'),
        'direction': row.get('direction'),
        'operator': row.get('operator'),
        'origin_name': display_name(row.get('origin_name')),
        'dest_name': display_name(row.get('dest_name')),
        'origin_aimed_dep': iso_from_millis(row.get('origin_aimed_dep')),
        'dest_aimed_arr': iso_from_millis(row.get('dest_aimed_arr')),
    }
    props.update(frag)
    return props, model, predicted




def stops_for(marts, operator, line, direction, day):
    """Stop sequence for a line, preferring the bus's own operator then '*',
    the live day_type then '*', and the feed's direction then its opposite.

    Known approximation: line_stops directions are inferred from journey
    geometry over historical pings that carried no direction column, so the
    mart's 'outbound' may be the feed's 'inbound' for any given line. Last
    resort is every stop on the line regardless of operator, direction or day,
    so a vocabulary mismatch degrades the answer instead of deleting it --
    this only triggers for a line line_stops has never observed under
    any grain at all, since the '*'/'*' row stops_index_key already falls
    back to exists for every line the mart has any data for."""
    key = stops_index_key(marts['stops'], operator, line, direction, day)
    if key:
        return marts['stops'][key]
    merged = []
    for (_op, ln, _d, _day), rows in marts['stops'].items():
        if ln == line:
            merged.extend(rows)
    merged.sort(key=lambda s: (s['seq'] is None, s['seq']))
    return merged


def find_last_stop(pings, stops):
    """Newest ping that sits within 100 m of a stop on this line."""
    for ping in pings:
        lat, lon = ping.get('lat'), ping.get('lon')
        if lat is None or lon is None:
            continue
        best = None
        for stop in stops:
            if stop.get('lat') is None:
                continue
            d = haversine_km(lat, lon, stop['lat'], stop['lon'])
            if d <= STOP_RADIUS_KM and (best is None or d < best[0]):
                best = (d, stop)
        if best:
            return best[1], ping.get('timestamp')
    return None, None


# --------------------------------------------------------------------------
# Bus routes
# --------------------------------------------------------------------------

@app.route('/buses', methods=['GET'])
def buses():
    try:
        rows = live_buses()
    except Exception as exc:
        log_once('qdb-buses', f'questdb unavailable: {exc}')
        return jsonify({"type": "FeatureCollection", "features": []}), 503

    marts = get_marts()
    now_s = time.time()
    features = []
    for row in rows:
        if not position_is_live(row, now_s):
            continue
        props, _model, _pred = bus_properties(row, marts, now_s)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row.get('lon'), row.get('lat')]},
            "properties": props,
        })
    return jsonify({"type": "FeatureCollection", "features": features})


@app.route('/bus/<bus_id>', methods=['GET'])
def bus_detail(bus_id):
    if not BUS_ID_RE.match(bus_id or ''):
        return jsonify({"error": "invalid bus_id"}), 400

    try:
        rows = live_buses(bus_id)
    except Exception as exc:
        log_once('qdb-bus', f'questdb unavailable: {exc}')
        return jsonify({"error": "upstream unavailable"}), 503
    if not rows:
        return jsonify({"error": "bus not live"}), 404

    row = rows[0]
    marts = get_marts()
    now_s = time.time()
    props, model, predicted = bus_properties(row, marts, now_s)

    props.update({
        'lat': row.get('lat'),
        'lon': row.get('lon'),
        'recorded_at': iso_from_millis(row.get('recorded_at')) or row.get('timestamp'),
        'scheduled_arrival_iso': iso_from_millis(row.get('dest_aimed_arr')),
        'predicted_arrival_iso': iso_from_epoch_s(predicted),
        'model': {
            'slope_s_per_km': model['slope_s_per_km'],
            'intercept_s': model['intercept_s'],
            'r2': model['r2'],
            'n_obs': model['n_obs'],
            'source': model['source'],
        } if model else None,
        'last_stop': None,
        'next_stop': None,
    })

    stops = stops_for(marts, row.get('operator'), row.get('line'), row.get('direction'),
                      day_type(local_dt(now_s)))
    if stops:
        try:
            pings = bus_history(bus_id)
        except Exception as exc:
            log_once('qdb-history', f'questdb history unavailable: {exc}')
            pings = []
        stop, ping_ts = find_last_stop(pings, stops)
        if stop:
            props['last_stop'] = {
                'name': stop['name'], 'locality': stop['locality'], 'atco': stop['atco'],
                'time_iso': ping_ts, 'lat': stop['lat'], 'lon': stop['lon'],
            }
            nxt = None
            if stop['seq'] is not None:
                later = [s for s in stops if s['seq'] is not None and s['seq'] > stop['seq']]
                nxt = min(later, key=lambda s: s['seq']) if later else None
            if nxt:
                # Prefer the observed hop time between these two stops. The ETA
                # regression carries a whole-journey intercept of ~12 minutes,
                # which is meaningless over the few hundred metres to the next
                # stop; the stop sequence already knows how long that hop takes.
                eta_iso = None
                hop = None
                if (nxt.get('secs_from_origin') is not None
                        and stop.get('secs_from_origin') is not None):
                    hop = float(nxt['secs_from_origin']) - float(stop['secs_from_origin'])
                    # Adjacent stops often share a median time, which would put
                    # the next arrival at the moment the bus left the last one.
                    if hop < MIN_STOP_HOP_S or hop > MAX_REMAINING_S:
                        hop = None
                if hop is not None:
                    # Measure from when the bus actually left the last stop, but
                    # never hand back an arrival that has already happened.
                    left = epoch_from_qdb_timestamp(ping_ts) if ping_ts else None
                    eta_iso = iso_from_epoch_s(max(now_s, (left or now_s) + hop))
                elif model and row.get('lat') is not None and nxt.get('lat') is not None:
                    km = (haversine_km(row['lat'], row['lon'], nxt['lat'], nxt['lon'])
                          * ROUTE_DETOUR_FACTOR)
                    secs = max(0.0, (model['slope_s_per_km'] or 0.0) * km)
                    if km <= MAX_REMAINING_KM and secs <= MAX_REMAINING_S:
                        eta_iso = iso_from_epoch_s(now_s + secs)
                props['next_stop'] = {
                    'name': nxt['name'], 'locality': nxt['locality'], 'atco': nxt['atco'],
                    'eta_iso': eta_iso,
                    # The SIRI VM feed carries aimed times only for the journey's
                    # origin and destination — there is no per-intermediate-stop
                    # schedule to compare against, so this stays null until a
                    # timetable (GTFS/TransXChange) source is ingested.
                    'aimed_iso': None,
                    'lat': nxt['lat'], 'lon': nxt['lon'],
                }

    return jsonify(props)


def road_snapped_geometry(stops):
    """[[lon, lat], ...] following real roads through these stops in order,
    via the self-hosted OSRM car profile (overview=full for the detailed
    shape, not OSRM's default simplified one). None on any failure --
    unreachable OSRM, a stop pair with no drivable path, a timeout -- so the
    caller can fall back to the straight stop-to-stop line rather than
    breaking the map."""
    coords = ';'.join('{:.6f},{:.6f}'.format(s['lon'], s['lat']) for s in stops)
    path = urllib.parse.quote(coords, safe=',;')
    url = f'{OSRM_URL}/route/v1/driving/{path}?overview=full&geometries=geojson&steps=false'
    try:
        with urllib.request.urlopen(url, timeout=OSRM_TIMEOUT_S) as resp:
            data = json.load(resp)
    except Exception as exc:
        log_once('osrm-route', f'OSRM unavailable, falling back to straight lines: {exc}')
        return None
    routes = data.get('routes') or []
    if data.get('code') != 'Ok' or not routes:
        return None
    return routes[0]['geometry']['coordinates']


def route_stops_for_line(marts, operator, line, direction, day):
    """Stop sequence plus how it was resolved, for /api/route.

    Reuses stops_index_key so the reported provenance always describes the
    SAME fallback stops_for() actually took -- there is no second, slightly
    different, resolution path to drift out of sync with it."""
    key = stops_index_key(marts['stops'], operator, line, direction, day)
    if key:
        resolved_operator, resolved_direction, resolved_day = key[0], key[2], key[3]
        if operator and resolved_operator != operator:
            source = 'pooled_operator'
        elif direction and resolved_direction != direction:
            source = ('opposite_direction' if resolved_day == day
                      else 'opposite_direction_day_agnostic')
        elif day and resolved_day != day:
            source = 'day_agnostic'
        else:
            source = 'exact'
        return marts['stops'][key], resolved_operator, resolved_direction, resolved_day, source

    merged = []
    for (_op, ln, _d, _day), rows in marts['stops'].items():
        if ln == line:
            merged.extend(rows)
    if not merged:
        return [], operator, direction, day, 'no_data'
    merged.sort(key=lambda s: (s['seq'] is None, s['seq']))
    return merged, None, None, None, 'line_merged_all_directions'


@app.route('/route', methods=['GET'])
def route():
    """GeoJSON for drawing a line on the map: its stop sequence as a dotted
    LineString, the stops themselves, and the buses currently working it.

    `line` is user-controlled via the query string and is interpolated into
    nothing here -- stop lookup is entirely in-memory against the marts, and
    the live-bus filter compares in Python -- but it is still whitelisted
    up front, same discipline as bus_id, because it flows into the response
    unexamined otherwise."""
    line = request.args.get('line', '')
    direction = request.args.get('direction') or None
    day = request.args.get('day_type') or None
    operator = request.args.get('operator') or None
    if not LINE_RE.match(line):
        return jsonify({"error": "invalid line"}), 400
    if direction is not None and not DIRECTION_RE.match(direction):
        return jsonify({"error": "invalid direction"}), 400
    if day is not None and day not in DAY_TYPES:
        return jsonify({"error": "invalid day_type"}), 400
    if operator is not None and not OPERATOR_RE.match(operator):
        return jsonify({"error": "invalid operator"}), 400

    marts = get_marts()
    now_s = time.time()
    if day is None:
        day = day_type(local_dt(now_s))

    stops, resolved_operator, resolved_direction, resolved_day, source = route_stops_for_line(
        marts, operator, line, direction, day)

    features = []
    route_note = None
    if not stops:
        route_note = (f"no stop sequence known for line '{line}'; the marts have "
                       "never observed it, or they are not built yet")
    else:
        usable = [s for s in stops if s.get('lat') is not None and s.get('lon') is not None]
        truncated = len(usable) > MAX_ROUTE_STOPS
        usable = usable[:MAX_ROUTE_STOPS]

        gap_after = [i for i, (a, b) in enumerate(zip(usable, usable[1:]))
                     if haversine_km(a['lat'], a['lon'], b['lat'], b['lon']) > MERGED_ROUTE_HOP_KM]
        merged_suspect = bool(gap_after)

        # A merged, reused line number draws as several disconnected runs,
        # split at the implausible gaps, rather than one line spanning every
        # town that number has ever been seen in. Each run is still fair game
        # for road-snapping below -- riders only board at a stop, never on
        # the road between two, so the exact path through even a merged stop
        # list is safe to estimate. merged_suspect (not geometry_quality)
        # is what tells the map this stop sequence itself is untrusted.
        segments = []
        start = 0
        for i in gap_after:
            segments.append(usable[start:i + 1])
            start = i + 1
        segments.append(usable[start:])
        segments = [seg for seg in segments if len(seg) >= 2]

        for seg_i, seg in enumerate(segments):
            seg_coords = [[s['lon'], s['lat']] for s in seg]
            seg_quality = 'stop_to_stop_straight_lines'

            cache_key = (resolved_operator, line, resolved_direction, resolved_day, seg_i)
            cache = marts['route_geom']
            if cache_key not in cache:
                cache[cache_key] = road_snapped_geometry(seg)
            road_coords = cache[cache_key]
            if road_coords:
                seg_coords = road_coords
                seg_quality = 'road_snapped'

            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": seg_coords},
                "properties": {
                    "kind": "route", "line": line, "operator": resolved_operator,
                    "direction": resolved_direction, "day_type": resolved_day,
                    "n_stops": len(seg), "source": source,
                    "geometry_quality": seg_quality,
                    "merged_suspect": merged_suspect,
                    "truncated": truncated,
                },
            })
        for stop in usable:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [stop['lon'], stop['lat']]},
                "properties": {
                    "kind": "stop", "atco": stop.get('atco'), "name": stop.get('name'),
                    "locality": stop.get('locality'), "seq": stop.get('seq'),
                    "median_secs_from_origin": stop.get('secs_from_origin'),
                },
            })
        if truncated:
            route_note = (f"line '{line}' resolved to more than {MAX_ROUTE_STOPS} stops and was "
                          "truncated; this is usually a reused service number "
                          "merging unrelated routes")

    try:
        for row in live_buses():
            if row.get('line') != line:
                continue
            if direction and row.get('direction') and row.get('direction') != direction:
                continue
            if not position_is_live(row, now_s):
                continue
            props, _model, _pred = bus_properties(row, marts, now_s)
            props['kind'] = 'bus'
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row.get('lon'), row.get('lat')]},
                "properties": props,
            })
    except Exception as exc:
        log_once('qdb-route-buses', f'questdb unavailable for /route: {exc}')

    payload = {"type": "FeatureCollection", "features": features}
    if route_note:
        payload["properties"] = {"note": route_note}
    return jsonify(payload)


@app.route('/search', methods=['GET'])
def search():
    raw = request.args.get('q', '')
    norm = normalise_query(raw)
    empty = {"query": raw, "matches": [], "lines": [], "bus_ids": []}
    if len(norm) < 2:
        return jsonify(empty)

    marts = get_marts()
    seen = set()
    matches = []
    for test in (lambda n: n == norm, lambda n: n.startswith(norm), lambda n: norm in n):
        for loc in marts['localities']:
            n = loc.get('locality_norm') or ''
            if n in seen or not test(n):
                continue
            seen.add(n)
            matches.append({
                'locality': loc.get('locality'), 'locality_norm': n,
                'lat': loc.get('lat'), 'lon': loc.get('lon'),
                'n_stops': loc.get('n_stops'),
            })
            if len(matches) >= 8:
                break
        if len(matches) >= 8:
            break

    lines = set()
    for m in matches:
        lines |= marts['line_loc'].get(m['locality_norm'], set())
    lines = sorted(x for x in lines if x)

    bus_ids = []
    if lines:
        try:
            wanted = set(lines)
            bus_ids = sorted({r['bus_id'] for r in live_buses() if r.get('line') in wanted})
        except Exception as exc:
            log_once('qdb-search', f'questdb unavailable for search: {exc}')

    return jsonify({"query": raw, "matches": matches, "lines": lines, "bus_ids": bus_ids})


# The marts are rebuilt by a nightly dbt/build.sh. Never assume they are fresh:
# report the age and flag it past a week, but keep serving predictions either
# way -- a stale regression beats no regression.
STALE_AFTER_HOURS = 7 * 24


def marts_age_hours(marts):
    if not marts.get('built_at'):
        return None
    built = epoch_from_qdb_timestamp(marts['built_at'])
    if built is None:
        return None
    return round(max(0.0, time.time() - built) / 3600.0, 2)


MARTS_NOTES = {
    MARTS_LOCKED: ('marts file at {path} is LOCKED by a writer -- a rebuild is in '
                   'progress. Transient, and not the same thing as a missing file.'),
    MARTS_MISSING: 'no marts file at {path}; predictions disabled',
    MARTS_UNREADABLE: 'marts at {path} could not be read; predictions disabled',
    MARTS_NO_DRIVER: 'duckdb driver not installed; predictions disabled',
}


@app.route('/model-info', methods=['GET'])
def model_info():
    marts = get_marts()
    age = marts_age_hours(marts)
    reload_state = marts.get('reload_state')
    if not marts['ok']:
        note = MARTS_NOTES.get(marts.get('state'),
                               'marts unavailable at {path}').format(path=MARTS_PATH)
        if marts.get('error'):
            note += ' ({})'.format(marts['error'])
    elif reload_state:
        # Serving the previous load because the newest one could not be read.
        note = ('serving the last good marts: the refresh failed ({}{}). '
                'Predictions continue from data built at {}.'.format(reload_state,
                   ': ' + marts['reload_error'] if marts.get('reload_error') else '',
                   marts['built_at']))
    elif not marts['n_models']:
        note = 'eta_models missing or empty; predictions disabled'
    elif age is not None and age > STALE_AFTER_HOURS:
        note = 'marts are %.1f days old; still serving predictions' % (age / 24.0)
    elif marts['day_types'] == ['*']:
        note = ('marts carry no day_type breakdown yet; every lookup resolves at '
                'the day-agnostic grain')
    else:
        note = None
    return jsonify({
        "built_at": marts['built_at'],
        "n_models": marts['n_models'],
        "duckdb": bool(marts['ok']),
        "day_types": marts['day_types'],
        # 'ok' | 'locked' | 'missing' | 'unreadable' | 'no_driver'. A locked
        # file is a rebuild in flight and must not read as a missing one.
        "marts_state": marts.get('state'),
        "refresh_failed": reload_state,
        "age_hours": age,
        "stale": bool(age is not None and age > STALE_AFTER_HOURS) or not marts['n_models'],
        "note": note,
    })


# 1.5.0's Android dashboard screen deliberately does not query QuestDB or
# Grafana directly -- it goes through here instead, reusing the exact SQL
# 1.4.0's Grafana panels already run, so there is one API surface for every
# client (web, Grafana, phone) rather than the phone learning QuestDB's wire
# protocol just to draw the same numbers a second way.

# DfT's Traffic Commissioner standard for local bus services -- see
# dbt/publish_punctuality_summary.py, which computes on_time_journeys with
# this same window before it ever reaches line_punctuality_summary. Not
# re-derived here: this endpoint reads the column QuestDB already has.
WORST_LINES_MIN_JOURNEYS = 50


@app.route('/metrics/summary', methods=['GET'])
def metrics_summary():
    """The five headline numbers from the dashboard's top row. Each is its
    own qdb_exec call and fails independently -- a QuestDB hiccup on one
    query shouldn't blank out the four that would have worked, the same
    reasoning bus_detail() already applies to last_stop/next_stop."""
    def scalar(sql, key):
        try:
            rows = qdb_rows(sql)
            return rows[0].get(key) if rows else None
        except Exception as exc:
            log_once(f'qdb-metrics-{key}', f'metrics query failed for {key}: {exc}')
            return None

    return jsonify({
        "feed_age_s": scalar(
            "select datediff('s', max(timestamp), now()) as v from bus_positions", 'v'),
        "buses_tracked_5m": scalar(
            "select count_distinct(bus_id) as v from bus_positions "
            "where timestamp > dateadd('m', -5, now())", 'v'),
        "positions_1h": scalar(
            "select count() as v from bus_positions "
            "where timestamp > dateadd('h', -1, now())", 'v'),
        "hours_since_build": scalar(
            "select datediff('h', max(timestamp), now()) as v from dbt_build_runs", 'v'),
        "dq_issues_last_build": scalar(
            "select last(tests_failed) + last(tests_warned) as v from dbt_build_runs", 'v'),
    })


@app.route('/metrics/punctuality', methods=['GET'])
def metrics_punctuality():
    """System-wide on-time %, delay and ETA model fit, plus the worst lines
    table -- the same rollup dbt/publish_punctuality_summary.py writes into
    QuestDB and the same queries the dashboard's 'Punctuality & ETA models'
    row runs. The per-line rollup is a volume-weighted approximation from
    grain-level medians, not a true per-journey percentage -- see that
    script's own docstring for what it does and doesn't measure."""
    try:
        system = qdb_rows(
            "select 100.0*sum(on_time_journeys)/sum(n_journeys) as pct_on_time, "
            "sum(median_delay_s*n_journeys)/sum(n_journeys) as median_delay_s "
            "from line_punctuality_summary "
            "where timestamp=(select max(timestamp) from line_punctuality_summary)"
        )
        model = qdb_rows(
            "select avg_r2, pct_weak_fit, avg_speed_kmh, n_models from eta_model_summary "
            "where timestamp=(select max(timestamp) from eta_model_summary)"
        )
        worst_lines = qdb_rows(
            "select line, n_journeys, 100.0*on_time_journeys/n_journeys as pct_on_time, "
            "median_delay_s, p90_delay_s from line_punctuality_summary "
            "where timestamp=(select max(timestamp) from line_punctuality_summary) "
            f"and n_journeys>={WORST_LINES_MIN_JOURNEYS} "
            "order by median_delay_s desc limit 25"
        )
    except Exception as exc:
        log_once('qdb-punctuality', f'punctuality metrics unavailable: {exc}')
        return jsonify({"error": "upstream unavailable"}), 503

    return jsonify({
        "system": system[0] if system else None,
        "model_fit": model[0] if model else None,
        "worst_lines": worst_lines,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
