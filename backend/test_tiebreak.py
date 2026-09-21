"""The pattern tiebreak (ADR-0008 (h)(i)): geometry, the 3 m lead rule, the
prior fallback, and the state rules (journey change, GTFS swap, dedupe)."""

import math
import time

import app
import tiebreak

import gtfs


# Two variants of one route along the same street, differing in the middle:
# P1 runs straight (lat 53.000); P2 detours ~445 m north (lat 53.004).
def _stops(mid_lat):
    return [{'atco': 'A', 'lat': 53.000, 'lon': -2.000},
            {'atco': 'M', 'lat': mid_lat, 'lon': -2.010},
            {'atco': 'C', 'lat': 53.000, 'lon': -2.020}]


P1 = {'pattern_id': 1, 'direction': 'outbound', 'trip_share': 0.8, 'is_circular': False,
      'stops': _stops(53.000)}
P2 = {'pattern_id': 2, 'direction': 'outbound', 'trip_share': 0.2, 'is_circular': False,
      'stops': _stops(53.004)}
V = ('OP', 'b1')
J = ('jr', 'A', 1000)


def _reset():
    tiebreak._trails.clear()
    tiebreak._pruned['at'] = 0.0


def test_distance_to_polyline():
    d = tiebreak.distance_m(53.0, -2.010, P1['stops'])
    assert d < 1.0                                       # on the straight line
    d = tiebreak.distance_m(53.004, -2.010, P1['stops'])
    assert 440 < d < 450                                 # ~445 m off it
    assert math.isinf(tiebreak.distance_m(53.0, -2.0, []))
    assert tiebreak.distance_m(53.0, -2.0, P1['stops'][:1]) < 1.0    # a single stop is a point
    dup = [{'lat': 53.0, 'lon': -2.0}, {'lat': 53.0, 'lon': -2.0}]    # zero-length segment
    assert tiebreak.distance_m(53.0, -2.0, dup) < 1.0


def test_clear_evidence_beats_the_prior():
    _reset()
    for i in range(5):                                    # bus follows the detour
        tiebreak.observe(V, J, 1, [P1, P2], 53.004, -2.010, f't{i}')
    best, resolved = tiebreak.choose(V, J, 1, [P1, P2])
    assert best is P2 and resolved
    # Bus on the straight variant: the prior, which is also right, and resolved.
    _reset()
    tiebreak.observe(V, J, 1, [P1, P2], 53.000, -2.010, 't0')
    best, resolved = tiebreak.choose(V, J, 1, [P1, P2])
    assert best is P1 and resolved


def test_no_evidence_or_a_close_call_falls_back_to_the_prior_flagged():
    _reset()
    assert tiebreak.choose(V, J, 1, [P1, P2]) == (P1, False)          # no trail at all
    # Bus on the shared stretch: both candidates equally close -> lead < 3 m.
    tiebreak.observe(V, J, 1, [P1, P2], 53.000, -2.000, 't0')
    assert tiebreak.choose(V, J, 1, [P1, P2]) == (P1, False)
    # Far from both: even a clear lead is uninformative above 60 m.
    _reset()
    near, far = dict(P1, stops=_stops(53.0008)), dict(P2, stops=_stops(53.0016))
    tiebreak.observe(V, J, 1, [near, far], 53.0100, -2.010, 't0')
    assert tiebreak.choose(V, J, 1, [near, far])[1] is False
    assert tiebreak.choose(V, J, 1, [P1]) == (P1, True)                # one candidate: no doubt


def test_trail_state_rules():
    _reset()
    for i in range(3):
        tiebreak.observe(V, J, 1, [P1, P2], 53.004, -2.010, f't{i}')
    assert tiebreak.choose(V, J, 1, [P1, P2])[0] is P2
    # A new journey, or a swapped GTFS file, starts from nothing.
    assert tiebreak.choose(V, ('jr2', 'A', 2000), 1, [P1, P2]) == (P1, False)
    assert tiebreak.choose(V, J, 2, [P1, P2]) == (P1, False)
    # The same ping seen again on the next poll is not counted twice.
    n = tiebreak._trails[V]['acc'][('outbound', 2)][1]
    tiebreak.observe(V, J, 1, [P1, P2], 53.004, -2.010, 't2')
    assert tiebreak._trails[V]['acc'][('outbound', 2)][1] == n
    # Seeded pings older than the first live ping count; newer ones do not.
    _reset()
    tiebreak.observe(V, J, 1, [P1, P2], 53.0, -2.01, 't5')
    tiebreak.observe(V, J, 1, [P1, P2], 53.0, -2.01, 't1', seeding=True)
    tiebreak.observe(V, J, 1, [P1, P2], 53.0, -2.01, 't9', seeding=True)
    assert tiebreak._trails[V]['acc'][('outbound', 1)][1] == 2
    # needs_seed fires once per trail.
    assert tiebreak.needs_seed(V, J, 1) and not tiebreak.needs_seed(V, J, 1)
    assert not tiebreak.needs_seed(('x', 'y'), J, 1)
    # Unusable pings and single-candidate calls are ignored.
    tiebreak.observe(('o', 'n'), J, 1, [P1, P2], None, None, 't')
    tiebreak.observe(('o', 'n'), J, 1, [P1], 53.0, -2.0, 't')
    assert ('o', 'n') not in tiebreak._trails


def test_prune_forgets_only_stale_vehicles():
    _reset()
    tiebreak.observe(V, J, 1, [P1, P2], 53.0, -2.01, 't0')
    tiebreak.observe(('o', 'old'), J, 1, [P1, P2], 53.0, -2.01, 't0')
    tiebreak._trails[('o', 'old')]['last_seen'] = time.time() - tiebreak.FORGET_S - 10
    tiebreak.prune()
    assert V in tiebreak._trails and ('o', 'old') not in tiebreak._trails


def _api_row(**kw):
    row = {'operator': 'OP', 'bus_id': 'b1', 'line': '88', 'direction': 'outbound',
           'origin_ref': 'A', 'dest_ref': 'C', 'journey_ref': 'jr',
           'origin_aimed_dep': 1000, 'lat': 53.004, 'lon': -2.010,
           'timestamp': '2026-09-21T10:00:00.000000Z'}
    row.update(kw)
    return row


def test_route_for_uses_the_trail_and_flags_the_prior():
    _reset()
    real = gtfs.candidates
    gtfs.candidates = lambda *a: [P1, P2]
    try:
        marts = dict(app.EMPTY_MARTS)
        stops, source = app.route_for(marts, _api_row(), time.time())
        assert source == 'gtfs_pattern_unresolved' and stops is P1['stops']
        for i in range(4):
            app.observe_trails([_api_row(timestamp=f'2026-09-21T10:0{i}:00Z')], sync_seed=True)
        stops, source = app.route_for(marts, _api_row(), time.time())
        assert source == 'gtfs_pattern' and stops is P2['stops']
    finally:
        gtfs.candidates = real


def test_seed_trails_backfills_from_questdb_and_survives_failure():
    _reset()
    real = (gtfs.candidates, app.qdb_rows, app.available_columns)
    gtfs.candidates = lambda *a: [P1, P2]
    app.available_columns = lambda: {'journey_ref', 'origin_aimed_dep', 'operator'}
    seen = []

    def fake_rows(sql):
        seen.append(sql)
        rows = [{'bus_id': 'b1', 'operator': 'OP', 'lat': 53.004, 'lon': -2.010,
                 'timestamp': f'2026-09-21T09:{i:02d}:00Z', 'journey_ref': 'jr',
                 'origin_aimed_dep': 1000} for i in range(60)]
        rows.append(dict(rows[0], journey_ref='older', lat=53.0))       # a previous journey
        rows.append(dict(rows[0], bus_id='other'))                       # someone else
        return rows
    app.qdb_rows = fake_rows
    try:
        app.observe_trails([_api_row()], sync_seed=True)
        acc = tiebreak._trails[('OP', 'b1')]['acc']
        assert 25 <= acc[('outbound', 2)][1] <= 32           # ~30 seeded pings + the live one
        assert acc[('outbound', 2)][0] / acc[('outbound', 2)][1] < 1.0     # all on the detour
        assert len(seen) == 1 and "'b1'" in seen[0]
        assert tiebreak.choose(('OP', 'b1'), journey := app.journey_key(_api_row()),
                               gtfs.generation, [P1, P2])[0] is P2, journey
        # A bus id that could break out of the SQL string is never queried.
        _reset()
        seen.clear()
        app.seed_trails([(('OP', "x'; drop"), J, [P1, P2])], 1)
        assert seen == []
        # QuestDB down: logged, not raised.
        _reset()

        def boom(sql):
            raise OSError('down')
        app.qdb_rows = boom
        app.observe_trails([_api_row()], sync_seed=True)
        # Only the live ping was scored: no backfill, no exception.
        assert tiebreak._trails[('OP', 'b1')]['acc'][('outbound', 2)][1] == 1
        # No journey columns in the table: nothing to backfill, no query.
        app.available_columns = lambda: {'operator'}
        seen.clear()
        app.qdb_rows = fake_rows
        app.seed_trails([(('OP', 'b1'), J, [P1, P2])], 1)
        assert seen == []
    finally:
        gtfs.candidates, app.qdb_rows, app.available_columns = real
