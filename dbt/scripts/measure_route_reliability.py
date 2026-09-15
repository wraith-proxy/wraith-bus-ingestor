#!/usr/bin/env python3
"""Re-measures line_stops' route-reliability, per the mart's own docstring
instruction: walk the published stops in seq order, sum the distance,
divide by the route's real length (estimated via OSRM road-snapping, the
same self-hosted OSRM instance /api/route already uses in production).

One-off diagnostic, not part of the build. Calls the LIVE public /api/route
endpoint (read-only GET) rather than hitting OSRM directly, since OSRM's
port isn't exposed to the host -- this reuses the exact same road-snapping
code path already serving the real map, road_snapped_geometry() in
backend/app.py, including its MAX_ROUTE_STOPS=400 truncation and
merged-line segment-splitting, rather than reimplementing either.

Usage: ../.venv/bin/python3 scripts/measure_route_reliability.py
"""
import json
import math
import urllib.parse
import urllib.request

import duckdb

API_BASE = 'https://slaycoach.quest/api'

CASES = [
    dict(label='coherent (single-operator tourist loop)',
         operator='KJTR', line='Golden_Tours_Hop_On_Hop_Off', direction='clockwise', day_type='*'),
    dict(label='reused across towns (pooled, no single operator)',
         operator='*', line='1', direction='outbound', day_type='*'),
]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def published_length_km(con, operator, line, direction, day_type):
    rows = con.execute(
        '''select lat, lon from line_stops
           where operator = ? and line = ? and direction = ? and day_type = ?
           order by seq''',
        [operator, line, direction, day_type],
    ).fetchall()
    total = sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(rows, rows[1:]))
    return total, len(rows)


def osrm_length_km(operator, line, direction, day_type):
    params = {'line': line, 'direction': direction, 'day_type': day_type}
    if operator != '*':
        params['operator'] = operator
    url = f'{API_BASE}/route?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'wraith-bus-ingestor-diagnostic/1.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    total = 0.0
    segments = 0
    for feature in data.get('features', []):
        geom = feature.get('geometry', {})
        if geom.get('type') != 'LineString':
            continue
        coords = geom.get('coordinates', [])
        if len(coords) < 2:
            continue
        segments += 1
        total += sum(
            haversine_km(a[1], a[0], b[1], b[0])
            for a, b in zip(coords, coords[1:])
        )
    return total, segments, data.get('properties', {}) if isinstance(data, dict) else {}


def main():
    con = duckdb.connect('wraith_bus_ingestor.duckdb', read_only=True)
    for case in CASES:
        print(f"\n=== {case['label']} ===")
        print(f"    operator={case['operator']} line={case['line']} "
              f"direction={case['direction']} day_type={case['day_type']}")
        pub_km, n_stops = published_length_km(
            con, case['operator'], case['line'], case['direction'], case['day_type'])
        print(f"    published (naive seq-order walk): {pub_km:.2f} km over {n_stops} stops")
        try:
            osrm_km, n_segments, _props = osrm_length_km(
                case['operator'], case['line'], case['direction'], case['day_type'])
            print(f"    OSRM road-snapped length: {osrm_km:.2f} km over {n_segments} segment(s)")
            if osrm_km > 0:
                print(f"    RATIO (published / real): {pub_km / osrm_km:.2f}x")
            else:
                print("    RATIO: undefined (zero-length OSRM result)")
        except Exception as exc:
            print(f"    OSRM/API call failed: {exc!r}")
    con.close()


if __name__ == '__main__':
    main()
