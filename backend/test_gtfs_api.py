"""The API's use of the GTFS reader: route resolution order, stop lookup,
/route, /search and /bus, against the tiny database test_gtfs builds."""

import os
import tempfile
import time

import app
from test_gtfs import _build, _point_at

import gtfs


def _marts():
    marts = dict(app.EMPTY_MARTS)
    marts.update({'stops': {}, 'stop_by_atco': {}, 'line_loc': {}, 'localities': [],
                  'route_geom': {}})
    return marts


def _row(**kw):
    row = {'operator': 'OP1', 'line': '88', 'direction': 'outbound',
           'origin_ref': 'A', 'dest_ref': 'C', 'lat': 53.0, 'lon': -2.0}
    row.update(kw)
    return row


def test_route_for_prefers_pinned_pattern_then_line_then_line_stops():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        marts = _marts()
        now = time.time()
        stops, source = app.route_for(marts, _row(), now)
        # Two patterns share these endpoints and there is no trail: prior, flagged.
        assert source == 'gtfs_pattern_unresolved'
        assert [s['atco'] for s in stops] == ['A', 'M1', 'C']
        one = _row(operator='OP2', origin_ref='X', dest_ref='Y')
        stops, source = app.route_for(marts, one, now)
        assert source == 'gtfs_pattern' and len(stops) == 2         # one candidate: no doubt
        # Endpoints the timetable has no pattern for: the line's primary pattern.
        stops, source = app.route_for(marts, _row(dest_ref='ZZ'), now)
        assert source == 'gtfs_line' and stops[0]['atco'] == 'A'
        # A line the timetable does not have (London): line_stops, while it exists.
        marts['stops'] = {('*', 'L9', 'outbound', '*'): [{'atco': 'T', 'seq': 1}]}
        stops, source = app.route_for(marts, _row(operator='TFLO', line='L9'), now)
        assert source == 'line_stops' and stops == [{'atco': 'T', 'seq': 1}]
        # ... and nothing at all once it is gone.
        stops, source = app.route_for(_marts(), _row(operator='TFLO', line='L9'), now)
        assert stops == [] and source == 'line_stops'


def test_stop_lookup_uses_naptan_then_line_stops():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        marts = _marts()
        marts['stop_by_atco'] = {'A': {'atco': 'A', 'lat': 1.0, 'lon': 1.0},
                                 'OLD': {'atco': 'OLD', 'lat': 2.0, 'lon': 2.0}}
        assert app.stop_lookup(marts, 'A')['lat'] == 53.6         # NaPTAN wins
        assert app.stop_lookup(marts, 'OLD')['lat'] == 2.0        # only line_stops has it
        assert app.stop_lookup(marts, 'NOPE') is None
        assert app.stop_lookup(marts, None) is None


def test_london_keeps_regression_eta_without_any_route():
    """TFLO has no timetable and, once line_stops is gone, no route. Its
    destination still resolves from NaPTAN, so the distance ETA survives."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        marts = _marts()
        marts['eta'] = {('*', '*', '*', '*', '*'): {
            'slope_s_per_km': 100.0, 'intercept_s': 0.0, 'r2': 0.5, 'n_obs': 10}}
        now = time.time()
        row = _row(operator='TFLO', line='L9', origin_ref='A', dest_ref='B',
                   lat=53.65, lon=-2.35)
        gtfs.prime(['A', 'B'])
        frag, _model, _pred = app.predict(row, marts, now)
        assert frag['remaining_km'] is not None and frag['eta_source'] == 'regression'


def test_route_endpoint_reads_the_timetable_first():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        real, real_marts, real_road = app.live_buses, app.get_marts, app.road_snapped_geometry
        app.live_buses = lambda *a, **k: []
        app.get_marts = _marts
        app.road_snapped_geometry = lambda stops: None
        try:
            body = app.app.test_client().get(
                '/route', query_string={'line': '88', 'operator': 'OP1',
                                        'direction': 'inbound'}).get_json()
            kinds = [f['properties']['kind'] for f in body['features']]
            assert kinds.count('stop') == 3 and 'route' in kinds
            route = next(f for f in body['features'] if f['properties']['kind'] == 'route')
            assert route['properties']['direction'] == 'inbound'
            assert route['properties']['source'] == 'exact'
            # No timetable and no marts: says so, still 200.
            body = app.app.test_client().get(
                '/route', query_string={'line': 'L9', 'operator': 'TFLO'}).get_json()
            assert 'timetable' in body['properties']['note']
        finally:
            app.live_buses, app.get_marts, app.road_snapped_geometry = real, real_marts, real_road


def test_search_unions_timetable_and_marts_lines():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        marts = _marts()
        marts['localities'] = [{'locality': 'Ashton', 'locality_norm': 'ASHTON',
                                'lat': 53.5, 'lon': -2.2, 'n_stops': 3}]
        marts['line_loc'] = {'ASHTON': {'L9'}}
        real, real_marts = app.live_buses, app.get_marts
        app.live_buses = lambda *a, **k: []
        app.get_marts = lambda: marts
        try:
            body = app.app.test_client().get('/search', query_string={'q': 'ashton'}).get_json()
            assert body['lines'] == ['88', '9', 'L9']
        finally:
            app.live_buses, app.get_marts = real, real_marts


def test_bus_detail_names_next_stop_from_the_timetable():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        row = _row(bus_id='b1', timestamp='2026-09-21T10:00:00.000000Z')
        # The fixture's stops sit at (53.0, -2.0); the bus has just left stop A.
        pings = [{'lat': 53.0, 'lon': -2.0, 'timestamp': '2026-09-21T09:59:00.000000Z'}]
        real = (app.live_buses, app.get_marts, app.bus_history)
        app.live_buses = lambda *a, **k: [row]
        app.get_marts = _marts
        app.bus_history = lambda *a, **k: pings
        try:
            body = app.app.test_client().get('/bus/b1').get_json()
            assert body['route_source'] == 'gtfs_pattern_unresolved'
            assert body['last_stop']['atco'] == 'A' and body['next_stop']['atco'] == 'M1'
        finally:
            app.live_buses, app.get_marts, app.bus_history = real


def test_everything_degrades_when_the_timetable_is_missing():
    _point_at('/nonexistent/gtfs.duckdb')
    marts = _marts()
    stops, source = app.route_for(marts, _row(), time.time())
    assert stops == [] and source == 'line_stops'
    assert app.stop_lookup(marts, 'A') is None
    app.prefetch_timetable([_row(), _row(origin_ref=None)])        # must not raise
    frag, _model, _pred = app.predict(_row(), marts, time.time())
    assert frag['eta_iso'] is None and frag['status'] == 'unknown'
