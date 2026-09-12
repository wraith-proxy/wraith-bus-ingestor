"""Plain-assert tests for the pure bus logic. Run: python test_bus_api.py

No pytest, no fixtures. Intended to run inside the api container, where flask
and duckdb are installed:  docker compose exec api python test_bus_api.py
"""

from datetime import datetime, timezone

import app


def test_haversine():
    # Same point.
    assert app.haversine_km(53.4, -2.2, 53.4, -2.2) == 0.0

    # One degree of latitude is ~111.19 km anywhere on the globe.
    d = app.haversine_km(53.0, -2.0, 54.0, -2.0)
    assert 111.0 < d < 111.4, d

    # Manchester Piccadilly -> Stockport is about 9 km.
    d = app.haversine_km(53.4774, -2.2309, 53.4084, -2.1494)
    assert 8.5 < d < 9.5, d

    # Symmetric, and longitude degrees shrink with latitude.
    a = app.haversine_km(53.4774, -2.2309, 53.4084, -2.1494)
    b = app.haversine_km(53.4084, -2.1494, 53.4774, -2.2309)
    assert abs(a - b) < 1e-9
    assert app.haversine_km(53.0, -2.0, 53.0, -1.0) < 111.0

    # 100 m stop radius: ~0.001 degrees of latitude is ~111 m.
    assert app.haversine_km(53.0, -2.0, 53.001, -2.0) > app.STOP_RADIUS_KM


def test_rag_boundaries():
    # UK PTI window: 1 min early to 5 min late inclusive is on time.
    assert app.classify_status(-61) == 'early'
    assert app.classify_status(-60) == 'on_time'
    assert app.classify_status(0) == 'on_time'
    assert app.classify_status(300) == 'on_time'
    assert app.classify_status(301) == 'late'
    assert app.classify_status(None) == 'unknown'
    # Well outside in both directions.
    assert app.classify_status(-3600) == 'early'
    assert app.classify_status(3600) == 'late'


def test_model_fallback_chain():
    """The 6-rung grain walk: (line, direction, day_type, hour_band) down to the
    global row, with the direction and day rungs both allowed to miss."""
    row = lambda tag: {'slope_s_per_km': 1.0, 'intercept_s': 0.0, 'r2': 0.5, 'n_obs': 10, 'tag': tag}
    full = {
        ('88', 'inbound', 'weekday', 'am_peak'): row('exact'),
        ('88', 'inbound', 'weekday', '*'): row('dir_day'),
        ('88', 'inbound', '*', 'am_peak'): row('dir_band'),
        ('88', 'inbound', '*', '*'): row('dir'),
        ('88', '*', '*', '*'): row('line'),
        ('*', '*', '*', '*'): row('global'),
    }
    pick = lambda models: app.pick_model(models, '88', 'inbound', 'weekday', 'am_peak')

    for key, source, tag in (
        (('88', 'inbound', 'weekday', 'am_peak'), 'line+direction+day+hour_band', 'exact'),
        (('88', 'inbound', 'weekday', '*'), 'line+direction+day', 'dir_day'),
        (('88', 'inbound', '*', 'am_peak'), 'line+direction+hour_band', 'dir_band'),
        (('88', 'inbound', '*', '*'), 'line+direction', 'dir'),
        (('88', '*', '*', '*'), 'line', 'line'),
        (('*', '*', '*', '*'), 'global', 'global'),
    ):
        m = pick(full)
        assert m['source'] == source and m['tag'] == tag, (m, source)
        del full[key]          # strip the rung we just matched, walk to the next
    assert pick(full) is None  # nothing left -> no model, not an exception

    # A day miss alone must not cost the hour_band grain: a Saturday bus on
    # marts that only fitted weekdays still gets the line+direction+hour_band
    # row rather than dropping all the way to the line.
    dayless = {('88', 'inbound', 'weekday', 'am_peak'): row('weekday_only'),
               ('88', 'inbound', '*', 'am_peak'): row('any_day'),
               ('88', '*', '*', '*'): row('line')}
    m = app.pick_model(dayless, '88', 'inbound', 'sat', 'am_peak')
    assert m['source'] == 'line+direction+hour_band' and m['tag'] == 'any_day'

    # Old-shape marts: every row filed under day_type '*'. Every lookup, on any
    # day, must still resolve -- just at the day-agnostic grain.
    old_shape = {('88', 'inbound', '*', 'am_peak'): row('old_band'),
                 ('88', 'inbound', '*', '*'): row('old_dir'),
                 ('*', '*', '*', '*'): row('old_global')}
    for day in ('weekday', 'sat', 'sun'):
        m = app.pick_model(old_shape, '88', 'inbound', day, 'am_peak')
        assert m['source'] == 'line+direction+hour_band' and m['tag'] == 'old_band', day
        m = app.pick_model(old_shape, '88', 'inbound', day, 'night')
        assert m['source'] == 'line+direction' and m['tag'] == 'old_dir', day

    # The marts' inferred direction may disagree with the feed's, and a bus may
    # carry no direction at all. Both fall through to the direction-agnostic
    # rungs rather than yielding nothing.
    mismatched = {('88', 'outbound', 'weekday', 'am_peak'): row('marts_dir'),
                  ('88', '*', '*', '*'): row('line')}
    assert app.pick_model(mismatched, '88', 'inbound', 'weekday', 'am_peak')['tag'] == 'line'
    assert app.pick_model(mismatched, '88', None, 'weekday', 'am_peak')['tag'] == 'line'

    # Unknown line still lands on the global row, and the picker never mutates
    # the marts it was handed.
    glob = {('*', '*', '*', '*'): row('global')}
    assert app.pick_model(glob, '999', 'outbound', 'sun', 'night')['source'] == 'global'
    assert 'source' not in glob[('*', '*', '*', '*')]


def test_stops_for_day_and_direction_fallback():
    """stops_for prefers the live day_type, then '*', then the opposite
    direction, then every stop on the line."""
    stop = lambda atco, lat: {'atco': atco, 'name': atco, 'locality': 'L',
                              'lat': lat, 'lon': -2.2, 'seq': 1}
    wk = [stop('WK', 53.40)]
    star = [stop('STAR', 53.41)]
    outb = [stop('OUT', 53.42)]
    marts = dict(app.EMPTY_MARTS)

    # Exact day wins over the '*' set.
    marts['stops'] = {('88', 'inbound', 'weekday'): wk, ('88', 'inbound', '*'): star}
    assert app.stops_for(marts, '88', 'inbound', 'weekday') == wk
    # A day the marts never fitted falls to '*', not to nothing.
    assert app.stops_for(marts, '88', 'inbound', 'sun') == star
    # Old-shape marts carry only '*': every day resolves there.
    marts['stops'] = {('88', 'inbound', '*'): star}
    for day in ('weekday', 'sat', 'sun'):
        assert app.stops_for(marts, '88', 'inbound', day) == star, day
    # Direction miss -> the opposite direction, preferring its exact day.
    marts['stops'] = {('88', 'outbound', 'weekday'): outb}
    assert app.stops_for(marts, '88', 'inbound', 'weekday') == outb
    # Neither direction vocabulary matches -> every stop on the line.
    marts['stops'] = {('88', 'up', 'weekday'): wk, ('88', 'down', 'sat'): outb}
    assert len(app.stops_for(marts, '88', 'inbound', 'weekday')) == 2
    # Nothing for the line at all.
    assert app.stops_for(marts, '999', 'inbound', 'weekday') == []


def test_marts_age_and_staleness():
    assert app.marts_age_hours({'built_at': None}) is None
    assert app.marts_age_hours({'built_at': 'rubbish'}) is None
    recent = app.iso_from_epoch_s(app.time.time() - 3600)
    age = app.marts_age_hours({'built_at': recent})
    assert 0.9 < age < 1.1, age
    old = app.iso_from_epoch_s(app.time.time() - 10 * 24 * 3600)
    assert app.marts_age_hours({'built_at': old}) > app.STALE_AFTER_HOURS


def test_query_normalisation():
    assert app.normalise_query('WILMSLOW') == 'WILMSLOW'
    assert app.normalise_query('  wilmslow  ') == 'WILMSLOW'
    assert app.normalise_query('Stockport, Cheshire') == 'STOCKPORT CHESHIRE'
    assert app.normalise_query("Stoke-on-Trent") == 'STOKE ON TRENT'
    assert app.normalise_query('newton   le   willows') == 'NEWTON LE WILLOWS'
    assert app.normalise_query("St. Helen's") == 'ST HELEN S'
    assert app.normalise_query('A6 corridor') == 'A6 CORRIDOR'
    assert app.normalise_query('') == ''
    assert app.normalise_query(None) == ''
    assert app.normalise_query('!!!') == ''


def test_hour_band():
    band = lambda h: app.hour_band(datetime(2026, 9, 12, h, 0, tzinfo=timezone.utc))
    assert band(3) == 'night'
    assert band(7) == 'am_peak'
    assert band(12) == 'inter'
    assert band(17) == 'pm_peak'
    assert band(20) == 'evening'
    assert band(23) == 'night'


def test_local_time_grain_under_bst_and_gmt():
    """hour_band and day_type MUST come from Europe/London, not UTC.

    Every one of these timestamps is chosen so that UTC gives the wrong answer
    during BST and the same answer during GMT -- so the assertions fail if the
    clock is ever read as UTC again, and the GMT half proves the offset is not
    simply hardcoded to +1."""
    assert str(app.local_zone()) == 'Europe/London', (
        'Europe/London did not load: the image needs the tzdata package')

    # --- BST (UTC+1) -----------------------------------------------------
    # 2026-07-03 23:30 UTC is Saturday 00:30 local. On UTC this files under
    # Friday, i.e. 'weekday' -- the exact bug the amendment calls out.
    d = app.local_dt(1783121400)
    assert d.hour == 0 and d.utcoffset().total_seconds() == 3600
    assert app.day_type(d) == 'sat'
    assert app.hour_band(d) == 'night'

    # 05:30 UTC is 06:30 local: 'am_peak' locally, 'night' on UTC.
    d = app.local_dt(1783143000)
    assert d.hour == 6
    assert app.hour_band(d) == 'am_peak'
    assert app.hour_band(datetime.fromtimestamp(1783143000, tz=timezone.utc)) == 'night'

    # Sunday 23:30 UTC is Monday 00:30 local -> 'weekday', not 'sun'.
    d = app.local_dt(1783294200)
    assert app.day_type(d) == 'weekday'

    # --- GMT (UTC+0, January) -------------------------------------------
    # Same clock times in winter: local == UTC, so the buckets must not shift.
    d = app.local_dt(1768001400)          # 2026-01-09 23:30 local, a Friday
    assert d.hour == 23 and d.utcoffset().total_seconds() == 0
    assert app.day_type(d) == 'weekday'
    assert app.hour_band(d) == 'night'

    d = app.local_dt(1768023000)          # 2026-01-10 05:30 local, a Saturday
    assert d.hour == 5
    assert app.day_type(d) == 'sat'
    assert app.hour_band(d) == 'night'    # 05:30 really is night in winter

    d = app.local_dt(1768174200)          # 2026-01-11 23:30 local, a Sunday
    assert app.day_type(d) == 'sun'

    # Bank holidays are deliberately not modelled: they file as 'weekday'.
    boxing_day = app.local_dt(datetime(2026, 12, 28, 12, 0,
                                       tzinfo=timezone.utc).timestamp())
    assert app.day_type(boxing_day) == 'weekday'


def test_display_name():
    assert app.display_name('Stockport_Interchange') == 'Stockport Interchange'
    assert app.display_name('Manchester Piccadilly') == 'Manchester Piccadilly'
    assert app.display_name(None) is None
    assert app.display_name('') is None


def test_time_helpers():
    assert app.iso_from_millis(0) == '1970-01-01T00:00:00Z'
    assert app.iso_from_millis(None) is None
    assert app.iso_from_millis('not-a-number') is None
    assert app.iso_from_epoch_s(None) is None
    assert app.epoch_from_qdb_timestamp('1970-01-01T00:00:00.000000Z') == 0.0
    assert app.epoch_from_qdb_timestamp(None) is None
    assert app.epoch_from_qdb_timestamp('rubbish') is None


def test_bus_id_whitelist():
    for good in ('ABC-1', 'SCMN_1234', 'bus.42', 'a:b'):
        assert app.BUS_ID_RE.match(good), good
    for bad in ("x' OR '1'='1", 'a b', "o'brien", 'a;drop', '', 'x' * 65, 'a%27'):
        assert not app.BUS_ID_RE.match(bad), bad


def test_predict_guards():
    """Absurd distances and journey remainders must degrade to 'unknown'."""
    marts = dict(app.EMPTY_MARTS)
    marts['eta'] = {('*', '*', '*', '*'): {'slope_s_per_km': 120.0, 'intercept_s': 60.0,
                                           'r2': 0.4, 'n_obs': 99}}
    # Destination stop 200 km away -> beyond MAX_REMAINING_KM.
    marts['stop_by_atco'] = {'FAR': {'atco': 'FAR', 'name': 'Far', 'locality': 'X',
                                     'lat': 55.3, 'lon': -2.2, 'seq': 1}}
    now = 1_757_600_000.0
    row = {'line': '88', 'direction': 'inbound', 'lat': 53.4, 'lon': -2.2,
           'dest_ref': 'FAR', 'dest_aimed_arr': int(now * 1000)}
    frag, model, pred = app.predict(row, marts, now)
    assert frag['status'] == 'unknown' and frag['eta_iso'] is None and frag['delay_s'] is None
    assert frag['remaining_km'] > app.MAX_REMAINING_KM
    assert model is not None  # a model matched; the guard rejected the distance

    # 50 km out at 120 s/km = 6000 s + 60 s -> over the 4 hour rail? No: 6060 s.
    marts['stop_by_atco'] = {'NEAR': {'atco': 'NEAR', 'name': 'Near', 'locality': 'X',
                                      'lat': 53.85, 'lon': -2.2, 'seq': 1}}
    row['dest_ref'] = 'NEAR'
    # Give it an origin too, so schedule adherence -- not the regression -- can
    # produce the delay. Still sitting at the origin with half the journey's
    # scheduled time already spent, so: late. (Not the WHOLE scheduled time:
    # that now trips the implausible-delay rail, which is its own test.)
    marts['stop_by_atco']['ORIG'] = {'atco': 'ORIG', 'name': 'Orig', 'locality': 'X',
                                     'lat': 53.4, 'lon': -2.2, 'seq': 0}
    row['origin_ref'] = 'ORIG'
    row['origin_aimed_dep'] = int((now - 1200) * 1000)
    row['dest_aimed_arr'] = int((now + 1200) * 1000)
    frag, _m, pred = app.predict(row, marts, now)
    assert frag['eta_iso'] is not None
    assert frag['delay_s'] > 0 and frag['status'] == 'late'
    assert frag['delay_source'] == 'progress_distance'
    assert pred > now

    # Same distance, a slope that pushes the remainder past 4 hours: the
    # regression is rejected, but adherence still answers, so the status stands.
    marts['eta'][('*', '*', '*', '*')]['slope_s_per_km'] = 400.0
    frag, _m, _p = app.predict(row, marts, now)
    assert frag['status'] == 'late' and frag['delay_source'] == 'progress_distance'
    # Drop the timetable and the regression alone cannot colour the map.
    frag, _m, _p = app.predict(dict(row, origin_aimed_dep=None), marts, now)
    assert frag['status'] == 'unknown' and frag['delay_s'] is None

    # No marts at all -> unknown, null everything, no exception.
    frag, model, pred = app.predict(row, dict(app.EMPTY_MARTS), now)
    assert frag == {'remaining_km': None, 'eta_iso': None, 'delay_s': None,
                    'delay_source': None, 'status': 'unknown'}
    assert model is None and pred is None


def test_marts_missing_is_not_fatal():
    """A path that is a directory (what Docker makes of a bind mount to a file
    that does not exist yet) must not blow up."""
    got = app._load_marts('/')
    assert got['ok'] is False and got['n_models'] == 0 and got['eta'] == {}
    got = app._load_marts('/no/such/marts.duckdb')
    assert got['ok'] is False and got['built_at'] is None


def test_find_last_stop_and_sequence():
    stops = [
        {'atco': 'A', 'name': 'Alpha', 'locality': 'L', 'lat': 53.400, 'lon': -2.200, 'seq': 1},
        {'atco': 'B', 'name': 'Bravo', 'locality': 'L', 'lat': 53.410, 'lon': -2.200, 'seq': 2},
        {'atco': 'C', 'name': 'Char', 'locality': 'L', 'lat': 53.420, 'lon': -2.200, 'seq': 3},
    ]
    pings = [  # newest first, as QuestDB returns them
        {'lat': 53.4150, 'lon': -2.2000, 'timestamp': '2026-09-12T10:05:00.000000Z'},
        {'lat': 53.4100, 'lon': -2.2001, 'timestamp': '2026-09-12T10:00:00.000000Z'},
        {'lat': 53.4000, 'lon': -2.2000, 'timestamp': '2026-09-12T09:55:00.000000Z'},
    ]
    stop, ts = app.find_last_stop(pings, stops)
    # The newest ping is mid-block (>100 m from anything); the next one is at B.
    assert stop['atco'] == 'B' and ts == '2026-09-12T10:00:00.000000Z'

    later = [s for s in stops if s['seq'] > stop['seq']]
    assert min(later, key=lambda s: s['seq'])['atco'] == 'C'

    # Nothing within 100 m anywhere.
    assert app.find_last_stop([{'lat': 0.0, 'lon': 0.0, 'timestamp': 'x'}], stops) == (None, None)
    assert app.find_last_stop([], stops) == (None, None)



def test_schedule_staleness_guard():
    """The feed republishes a parked vehicle's last journey, so an aimed time
    from yesterday must not be read as a 24-hour delay."""
    now = 1789200000.0
    fresh = app.schedule_epoch_s((now + 600) * 1000, now,
                                 app.MAX_SCHEDULE_AGE_S, app.MAX_SCHEDULE_AHEAD_S)
    assert fresh == now + 600
    # Just inside and just outside the staleness rail.
    assert app.schedule_epoch_s((now - app.MAX_SCHEDULE_AGE_S + 60) * 1000, now,
                                app.MAX_SCHEDULE_AGE_S, app.MAX_SCHEDULE_AHEAD_S) is not None
    assert app.schedule_epoch_s((now - app.MAX_SCHEDULE_AGE_S - 60) * 1000, now,
                                app.MAX_SCHEDULE_AGE_S, app.MAX_SCHEDULE_AHEAD_S) is None
    # Yesterday's schedule -- the case that dominated the live late count.
    assert app.schedule_epoch_s((now - 86400) * 1000, now,
                                app.MAX_SCHEDULE_AGE_S, app.MAX_SCHEDULE_AHEAD_S) is None
    # Absurdly far ahead, and junk.
    assert app.schedule_epoch_s((now + 86400) * 1000, now,
                                app.MAX_SCHEDULE_AGE_S, app.MAX_SCHEDULE_AHEAD_S) is None
    assert app.schedule_epoch_s(None, now, 1, 1) is None
    assert app.schedule_epoch_s('nonsense', now, 1, 1) is None


def _grid_marts():
    """Marts holding one line with two stops, indexed the way _load_marts does."""
    marts = dict(app.EMPTY_MARTS)
    a = {'atco': 'A', 'name': 'Alpha', 'locality': 'Ashton', 'lat': 53.500,
         'lon': -2.200, 'seq': 1, 'secs_from_origin': 300.0}
    m = {'atco': 'M', 'name': 'Mid', 'locality': 'Ashton', 'lat': 53.505,
         'lon': -2.200, 'seq': 2, 'secs_from_origin': 450.0}
    b = {'atco': 'B', 'name': 'Bravo', 'locality': 'Ashton', 'lat': 53.510,
         'lon': -2.200, 'seq': 3, 'secs_from_origin': 600.0}
    marts['stops'] = {('88', 'outbound', 'weekday'): [a, m, b]}
    marts['stop_grid'] = {}
    marts['stop_by_line_atco'] = {}
    for key, stops in marts['stops'].items():
        cells = marts['stop_grid'].setdefault(key, {})
        by_atco = marts['stop_by_line_atco'].setdefault(key, {})
        for st in stops:
            by_atco.setdefault(st['atco'], st)
            cells.setdefault((round(st['lat'], app.GRID), round(st['lon'], app.GRID)), []).append(st)
    return marts, a, m, b


def test_nearest_stop_grid():
    marts, a, _m, _b = _grid_marts()
    got, km = app.nearest_stop(marts, '88', 'outbound', 'weekday', 53.5001, -2.2001)
    assert got is a and km is not None and km < 0.05
    # Beyond the match radius -> no stop rather than a wrong one.
    got, km = app.nearest_stop(marts, '88', 'outbound', 'weekday', 53.9, -2.2)
    assert got is None and km is None
    # Unknown direction falls through to the opposite one.
    got, _km = app.nearest_stop(marts, '88', 'inbound', 'weekday', 53.5001, -2.2001)
    assert got is a
    # Unknown line, and missing coordinates, are both survivable.
    assert app.nearest_stop(marts, 'NOPE', 'outbound', 'weekday', 53.5, -2.2) == (None, None)
    assert app.nearest_stop(marts, '88', 'outbound', 'weekday', None, None) == (None, None)


def test_progress_delay():
    """Delay is the observed route shape stretched onto this journey's own
    timetable, so the origin/destination anchors cancel."""
    marts, a, m, b = _grid_marts()
    now = 1789200000.0
    # Route shape: origin A at 300 s, mid M at 450 s, destination B at 600 s,
    # so the span is 300 s and M sits at fraction 0.5.
    # Timetable: depart now-600, arrive now+600 -> 1200 s scheduled.
    base = {'line': '88', 'direction': 'outbound',
            'origin_ref': 'A', 'dest_ref': 'B',
            'origin_aimed_dep': (now - 600) * 1000,
            'dest_aimed_arr': (now + 600) * 1000}

    # Sitting at the origin: fraction 0, so expected elapsed 0 but 600 s gone.
    row = dict(base)
    delay, stop = app.progress_delay(row, marts, now, '88', 'outbound', 'weekday',
                                     53.5001, -2.2001)
    assert stop['atco'] == 'A' and delay == 600
    assert app.classify_status(delay) == 'late'

    # Halfway along at the halfway point of the timetable: bang on time.
    delay, stop = app.progress_delay(row, marts, now, '88', 'outbound', 'weekday',
                                     53.5051, -2.2001)
    assert stop['atco'] == 'M' and delay == 0
    assert app.classify_status(delay) == 'on_time'

    # At the destination the journey is over: no honest reading is possible, so
    # None rather than a delay that keeps growing while the bus sits there.
    delay, stop = app.progress_delay(row, marts, now, '88', 'outbound', 'weekday',
                                     53.5101, -2.2001)
    assert stop['atco'] == 'B' and delay is None
    assert app.classify_status(delay) == 'unknown'

    # The anchor cancels: shifting every stop's secs_from_origin by +1000 must
    # not change the answer, since only the fraction between them is used.
    for st in (a, m, b):
        st['secs_from_origin'] += 1000
    shifted, _stop = app.progress_delay(row, marts, now, '88', 'outbound', 'weekday',
                                        53.5051, -2.2001)
    assert shifted == 0
    for st in (a, m, b):
        st['secs_from_origin'] -= 1000

    # Guards, each returning nothing rather than a wrong number.
    row = dict(base, origin_aimed_dep=(now + 600) * 1000)   # not departed yet
    assert app.progress_delay(row, marts, now, '88', 'outbound', 'weekday', 53.5001, -2.2001)[0] is None
    row = dict(base, origin_aimed_dep=(now - 86400) * 1000)  # yesterday's journey
    assert app.progress_delay(row, marts, now, '88', 'outbound', 'weekday', 53.5001, -2.2001)[0] is None
    row = dict(base, dest_aimed_arr=None)                    # no arrival time
    assert app.progress_delay(row, marts, now, '88', 'outbound', 'weekday', 53.5001, -2.2001)[0] is None
    row = dict(base, origin_ref='NOSUCH')                    # origin not on the line
    assert app.progress_delay(row, marts, now, '88', 'outbound', 'weekday', 53.5001, -2.2001)[0] is None
    row = dict(base, dest_ref='A')                           # zero-length span
    assert app.progress_delay(row, marts, now, '88', 'outbound', 'weekday', 53.5001, -2.2001)[0] is None
    # Nowhere near a stop -> no reading, so predict() falls back to the model.
    assert app.progress_delay(dict(base), marts, now, '88', 'outbound', 'weekday', 53.9, -2.9)[0] is None


def test_arrived_vehicles_stop_accruing_delay():
    """A bus that reached its destination and parked must not keep getting
    later. We cannot say WHEN it arrived from one snapshot, so we say nothing.

    Measured live: 192 of 2434 rated vehicles, e.g. a line 411 working
    scheduled 09:45-10:30 reading 35 minutes late while stood on the stand."""
    now = 1789200000.0
    marts = dict(app.EMPTY_MARTS)
    marts['stop_by_atco'] = {
        'O': {'atco': 'O', 'name': 'Origin', 'locality': 'X', 'lat': 53.40,
              'lon': -2.20, 'seq': 1, 'secs_from_origin': 0.0},
        'D': {'atco': 'D', 'name': 'Dest', 'locality': 'Y', 'lat': 53.60,
              'lon': -2.20, 'seq': 9, 'secs_from_origin': 1200.0},
    }
    # Scheduled 09:45-10:30-ish: departed 45 min ago, due 10 min ago.
    base = {'origin_ref': 'O', 'dest_ref': 'D',
            'origin_aimed_dep': (now - 2700) * 1000,
            'dest_aimed_arr': (now - 600) * 1000}

    # Parked exactly on the destination stop.
    assert app.geometric_progress_delay(dict(base, lat=53.60, lon=-2.20), marts, now) is None
    # ...and within ARRIVED_KM of it. 0.002 degrees of latitude is ~222 m.
    near = dict(base, lat=53.598, lon=-2.20)
    assert app.haversine_km(53.598, -2.20, 53.60, -2.20) < app.ARRIVED_KM
    assert app.geometric_progress_delay(near, marts, now) is None

    # Still genuinely running 1.1 km short of the terminus: rated, not refused.
    running = dict(base, lat=53.59, lon=-2.20)
    assert app.haversine_km(53.59, -2.20, 53.60, -2.20) > app.ARRIVED_KM
    # 95% of the way through a 35 minute timetable with 45 minutes gone.
    delay = app.geometric_progress_delay(running, marts, now)
    assert delay is not None and 650 < delay < 760, delay

    # The fraction rail catches the same thing proportionally, which is what
    # protects short routes where 250 m is most of the journey.
    assert app._delay_from_fraction(app.ARRIVED_FRACTION, now - 600, 1200, now) is None
    assert app._delay_from_fraction(app.ARRIVED_FRACTION - 0.01, now - 600, 1200, now) is not None

    # predict() must refuse regardless of WHICH method would have answered. The
    # stop-sequence method measures progress in median seconds, so it can sit
    # below ARRIVED_FRACTION while the bus is physically parked at the terminus
    # -- 10 live vehicles were being rated that way.
    gm, a, mid, b = _grid_marts()
    gm['stop_by_atco'] = {'A': a, 'B': b}
    parked = {'line': '88', 'direction': 'outbound', 'lat': 53.5100, 'lon': -2.2000,
              'origin_ref': 'A', 'dest_ref': 'B',
              'origin_aimed_dep': (now - 2700) * 1000,
              'dest_aimed_arr': (now - 600) * 1000}
    frag, _model, _pred = app.predict(parked, gm, now)
    assert frag['remaining_km'] is not None and frag['remaining_km'] <= app.ARRIVED_KM
    assert frag['delay_s'] is None and frag['delay_source'] is None
    assert frag['status'] == 'unknown'
    # The same bus a kilometre short of the terminus, on a timetable it can
    # plausibly still be running to, is rated as normal.
    running_bus = dict(parked, lat=53.5010,
                       origin_aimed_dep=(now - 600) * 1000,
                       dest_aimed_arr=(now + 600) * 1000)
    frag, _model, _pred = app.predict(running_bus, gm, now)
    assert frag['delay_s'] is not None and frag['delay_source'] is not None


def test_implausible_delay_is_refused():
    """Past a fraction of its own scheduled duration, a "very late" bus and a
    vehicle the feed has left logged onto a finished journey look identical from
    one snapshot, so we refuse rather than publish a confident wrong number.

    Traced live: line 391 / WX75_OBR read 104 minutes late on a 92 minute
    working; its QuestDB track shows it finished that working and is part-way
    through a later one, with the old journey's aimed times still attached."""
    now = 1789200000.0
    limit = lambda sched: max(app.STALE_JOURNEY_FLOOR_S, app.STALE_JOURNEY_FRACTION * sched)
    # fraction 0 means the reported delay is exactly the time since departure.
    at_origin = lambda delay, sched: app._delay_from_fraction(0.0, now - delay, sched, now)

    # A long working: the fraction binds, the floor does not.
    sched = 5520.0                       # 92 minutes, as on the live example
    assert limit(sched) == app.STALE_JOURNEY_FRACTION * sched
    assert at_origin(6263, sched) is None          # the 104-minute reading
    assert at_origin(4417, sched) is None          # the 74-minute one
    assert at_origin(int(limit(sched)), sched) == int(limit(sched))      # boundary kept
    assert at_origin(int(limit(sched)) + 1, sched) is None               # one past it

    # A short working: the floor binds, so a genuinely late little route is not
    # held to an implausibly tight bound.
    sched = 900.0                        # 15 minutes
    assert limit(sched) == app.STALE_JOURNEY_FLOOR_S
    assert at_origin(880, sched) == 880             # 14.7 min late on 15: kept
    assert at_origin(901, sched) is None
    # The old rule was "delay > scheduled duration", which let all of these
    # through -- a bus part-way into its next trip always looks like it made
    # some progress, so it never triggered.
    assert 6263 < 92 * 60 * 2 and at_origin(6263, 5520.0) is None

    # Normal running buses are untouched by either rail.
    assert app._delay_from_fraction(0.5, now - 600, 1200, now) == 0
    assert app._delay_from_fraction(0.25, now - 600, 1200, now) == 300

    # ...and the same end to end through the geometric method.
    marts = dict(app.EMPTY_MARTS)
    marts['stop_by_atco'] = {
        'O': {'atco': 'O', 'name': 'Origin', 'locality': 'X', 'lat': 53.40,
              'lon': -2.20, 'seq': 1, 'secs_from_origin': 0.0},
        'D': {'atco': 'D', 'name': 'Dest', 'locality': 'Y', 'lat': 53.60,
              'lon': -2.20, 'seq': 9, 'secs_from_origin': 1200.0},
    }
    stale = {'origin_ref': 'O', 'dest_ref': 'D', 'lat': 53.40, 'lon': -2.20,
             'origin_aimed_dep': (now - 6263) * 1000,
             'dest_aimed_arr': (now - 6263 + 5520) * 1000}
    assert app.geometric_progress_delay(stale, marts, now) is None
    ok = dict(stale, origin_aimed_dep=(now - 900) * 1000,
              dest_aimed_arr=(now + 1680) * 1000)
    delay = app.geometric_progress_delay(ok, marts, now)
    assert delay == 900 and app.classify_status(delay) == 'late'


def test_detour_factor_applied():
    """predict() must hand the model along-route km, not the straight line."""
    marts = dict(app.EMPTY_MARTS)
    marts['eta'] = {('*', '*', '*', '*'): {'line': '*', 'direction': '*',
                                           'day_type': '*', 'hour_band': '*',
                                      'slope_s_per_km': 100.0, 'intercept_s': 0.0,
                                      'r2': 0.5, 'n_obs': 1000, 'source': 'global'}}
    marts['stop_by_atco'] = {'D': {'atco': 'D', 'name': 'Dest', 'locality': 'X',
                                   'lat': 53.6, 'lon': -2.2, 'seq': 9,
                                   'secs_from_origin': None}}
    now = 1789200000.0
    row = {'line': '88', 'direction': 'outbound', 'lat': 53.5, 'lon': -2.2,
           'dest_ref': 'D', 'dest_aimed_arr': (now + 3600) * 1000}
    straight = app.haversine_km(53.5, -2.2, 53.6, -2.2)
    frag, _m, _p = app.predict(row, marts, now)
    assert abs(frag['remaining_km'] - straight * app.ROUTE_DETOUR_FACTOR) < 0.01
    assert frag['remaining_km'] > straight   # scaled up, never down
    # No stop match and no origin_ref, so no schedule-adherence reading at all:
    # the regression supplies the ETA but never the delay.
    assert frag['delay_source'] is None and frag['delay_s'] is None
    assert frag['eta_iso'] is not None


def test_position_is_live():
    """Freshness must be judged on RecordedAtTime: a replayed dead record is
    re-ingested every 30 s and so always looks newly arrived."""
    now = 1789200000.0
    assert app.position_is_live({'recorded_at': (now - 30) * 1000}, now) is True
    assert app.position_is_live({'recorded_at': (now - app.MAX_POSITION_AGE_S + 30) * 1000}, now) is True
    assert app.position_is_live({'recorded_at': (now - app.MAX_POSITION_AGE_S - 30) * 1000}, now) is False
    assert app.position_is_live({'recorded_at': (now - 86400) * 1000}, now) is False
    # No recorded_at, or junk: fall back to the ingest window rather than drop.
    assert app.position_is_live({}, now) is True
    assert app.position_is_live({'recorded_at': None}, now) is True
    assert app.position_is_live({'recorded_at': 'nope'}, now) is True


def test_geometric_progress_delay():
    """Progress measured as distance covered between the two termini -- the wide
    coverage path, since it needs only the origin and destination coordinates."""
    now = 1789200000.0
    marts = dict(app.EMPTY_MARTS)
    marts['stop_by_atco'] = {
        'O': {'atco': 'O', 'name': 'Origin', 'locality': 'X', 'lat': 53.40, 'lon': -2.20,
              'seq': 1, 'secs_from_origin': 0.0},
        'D': {'atco': 'D', 'name': 'Dest', 'locality': 'Y', 'lat': 53.60, 'lon': -2.20,
              'seq': 9, 'secs_from_origin': 1200.0},
    }
    base = {'origin_ref': 'O', 'dest_ref': 'D',
            'origin_aimed_dep': (now - 600) * 1000,
            'dest_aimed_arr': (now + 600) * 1000}   # 1200 s scheduled

    # Halfway in distance, halfway through the scheduled time: on time.
    row = dict(base, lat=53.50, lon=-2.20)
    assert app.geometric_progress_delay(row, marts, now) == 0

    # Still at the origin after 600 s: 10 minutes late.
    row = dict(base, lat=53.40, lon=-2.20)
    assert app.geometric_progress_delay(row, marts, now) == 600

    # Nearly there and 10 minutes early -- still running, so still rated.
    row = dict(base, lat=53.59, lon=-2.20)
    delay = app.geometric_progress_delay(row, marts, now)
    assert delay is not None and -600 < delay < -400, delay

    # Actually at the destination: the journey is over and the delay would grow
    # for as long as it sits there, so nothing is reported. See
    # test_arrived_vehicles_stop_accruing_delay.
    row = dict(base, lat=53.60, lon=-2.20)
    assert app.geometric_progress_delay(row, marts, now) is None
    row = dict(base, lat=53.60, lon=-2.20,
               origin_aimed_dep=(now - 1200) * 1000, dest_aimed_arr=now * 1000)
    assert app.geometric_progress_delay(row, marts, now) is None

    # Guards.
    assert app.geometric_progress_delay(dict(base, lat=53.5, lon=-2.2,
                                             dest_aimed_arr=None), marts, now) is None
    assert app.geometric_progress_delay(dict(base, lat=53.5, lon=-2.2,
                                             origin_ref='NOPE'), marts, now) is None
    assert app.geometric_progress_delay(dict(base, lat=None, lon=None), marts, now) is None
    # Circular route: origin and destination are the same stop, so the fraction
    # would sit at 0.5 all the way round and the bus would read ever later.
    circ = dict(base, lat=53.40, lon=-2.20, dest_ref='O')
    assert app.geometric_progress_delay(circ, marts, now) is None
    # ...and the same when it is a loop back to a *nearby* stop rather than the
    # identical one, which is how most city-centre circulars are published, and
    # even when the bus is far from both termini so the old total-distance
    # guard would have let it through.
    marts['stop_by_atco']['D2'] = {'atco': 'D2', 'name': 'Near Origin', 'locality': 'X',
                                   'lat': 53.4045, 'lon': -2.20, 'seq': 9,
                                   'secs_from_origin': 1200.0}
    assert app.haversine_km(53.40, -2.20, 53.4045, -2.20) < app.MIN_TERMINUS_SEPARATION_KM
    loop = dict(base, lat=53.50, lon=-2.20, dest_ref='D2')   # 11 km from both
    assert app.geometric_progress_delay(loop, marts, now) is None
    # Stale timetable.
    assert app.geometric_progress_delay(
        dict(base, lat=53.5, lon=-2.2, origin_aimed_dep=(now - 86400) * 1000,
             dest_aimed_arr=(now - 86000) * 1000), marts, now) is None


def test_delay_never_comes_from_the_regression():
    """The ETA regression is ~7 minutes biased late at the destination, so it
    must supply eta_iso and never delay_s/status."""
    now = 1789200000.0
    marts = dict(app.EMPTY_MARTS)
    marts['eta'] = {('*', '*', '*', '*'): {'line': '*', 'direction': '*',
                                           'day_type': '*', 'hour_band': '*',
                                      'slope_s_per_km': 180.0, 'intercept_s': 750.0,
                                      'r2': 0.7, 'n_obs': 5000, 'source': 'global'}}
    marts['stop_by_atco'] = {'D': {'atco': 'D', 'name': 'Dest', 'locality': 'Y',
                                   'lat': 53.60, 'lon': -2.20, 'seq': 9,
                                   'secs_from_origin': None}}
    # Destination known, but no origin_ref -> no adherence reading is possible.
    row = {'line': '88', 'direction': 'outbound', 'lat': 53.50, 'lon': -2.20,
           'dest_ref': 'D', 'dest_aimed_arr': (now + 600) * 1000,
           'origin_aimed_dep': (now - 600) * 1000}
    frag, model, predicted = app.predict(row, marts, now)
    assert model is not None and frag['eta_iso'] is not None and predicted > now
    assert frag['delay_s'] is None
    assert frag['delay_source'] is None
    assert frag['status'] == 'unknown'


def test_next_stop_hop_guard():
    """MIN_STOP_HOP_S exists because adjacent stops routinely share a median
    time-from-origin, which would date the next arrival to the moment the bus
    left the previous stop."""
    assert app.MIN_STOP_HOP_S > 0
    for a, b in ((300.0, 300.0), (300.0, 320.0), (300.0, 250.0)):
        hop = b - a
        assert hop < app.MIN_STOP_HOP_S      # all rejected as unusable
    assert (600.0 - 300.0) >= app.MIN_STOP_HOP_S   # a real hop survives


def test_marts_state_distinguishes_locked_from_missing():
    """A rebuild in progress and a file that is not there must not look alike to
    anyone reading /api/model-info, and a failed refresh must not blank out an
    API that already has good marts loaded."""
    import os
    import tempfile

    # No file, and the directory Docker creates when you bind-mount a file that
    # does not exist yet: both 'missing'.
    assert app._load_marts('/no/such/marts.duckdb')['state'] == app.MARTS_MISSING
    assert app._load_marts('/')['state'] == app.MARTS_MISSING

    if app.duckdb is not None:
        # Something that is not a DuckDB database at all: 'unreadable', which is
        # a real problem, not a transient one.
        fd, junk = tempfile.mkstemp(suffix='.duckdb')
        try:
            os.write(fd, b'this is not a duckdb file')
            os.close(fd)
            got = app._load_marts(junk)
            assert got['state'] == app.MARTS_UNREADABLE, got['state']
            assert got['ok'] is False and got['error']
        finally:
            os.unlink(junk)

    # Every state carries its own note, and they are not interchangeable.
    notes = {k: v for k, v in app.MARTS_NOTES.items()}
    assert notes[app.MARTS_LOCKED] != notes[app.MARTS_MISSING]
    assert 'LOCKED' in notes[app.MARTS_LOCKED]
    assert len(set(notes.values())) == len(notes)

    # A failed refresh keeps the last good marts rather than dropping every bus
    # to 'unknown' for the length of a rebuild.
    saved = dict(app._marts_state)
    original_loader = app._load_marts
    try:
        good = dict(app.EMPTY_MARTS, ok=True, state=app.MARTS_OK, n_models=7,
                    built_at='2026-09-12T10:00:00Z')
        queued = [good]
        app._load_marts = lambda path: queued.pop(0)
        app._marts_state.update({'data': None, 'mtime': None, 'checked': 0.0})

        m = app.get_marts()
        assert m['ok'] and m['n_models'] == 7 and m['state'] == app.MARTS_OK
        assert m.get('reload_state') is None

        # Force a re-check whose load fails with a lock.
        queued.append(dict(app.EMPTY_MARTS, state=app.MARTS_LOCKED,
                           error='Conflicting lock is held'))
        app._marts_state['checked'] = 0.0
        app._marts_state['mtime'] = 'force-a-reload'
        m = app.get_marts()
        assert m['ok'] is True and m['n_models'] == 7, 'last good marts were dropped'
        assert m['reload_state'] == app.MARTS_LOCKED
        assert 'lock' in m['reload_error'].lower()
        # The failed load must not be cached as if it had worked, or the marts
        # would never be picked up again after the writer lets go.
        assert app._marts_state['mtime'] is None
    finally:
        app._load_marts = original_loader
        app._marts_state.clear()
        app._marts_state.update(saved)


def test_load_marts_tolerates_both_shapes():
    """The dbt marts are rebuilt independently, so for a while ghost_bus.duckdb
    still has the pre-day_type columns. Both shapes must load; the old one just
    files everything under day_type '*'."""
    if app.duckdb is None:                      # pragma: no cover
        print('   (skipped: no duckdb driver)')
        return
    import shutil
    import tempfile

    def build(path, with_day):
        con = app.duckdb.connect(path)
        if with_day:
            con.execute('CREATE TABLE mart_eta_model(line VARCHAR, direction VARCHAR, '
                        'day_type VARCHAR, hour_band VARCHAR, slope_s_per_km DOUBLE, '
                        'intercept_s DOUBLE, r2 DOUBLE, n_obs BIGINT)')
            con.execute("INSERT INTO mart_eta_model VALUES "
                        "('88','outbound','weekday','am_peak',150,90,0.7,400),"
                        "('88','outbound','sat','*',160,80,0.6,300),"
                        "('*','*','*','*',170,70,0.5,9000)")
            con.execute('CREATE TABLE mart_line_stops(line VARCHAR, direction VARCHAR, '
                        'day_type VARCHAR, atco_code VARCHAR, stop_name VARCHAR, '
                        'locality VARCHAR, lat DOUBLE, lon DOUBLE, seq INTEGER, '
                        'median_secs_from_origin DOUBLE, n_obs BIGINT)')
            con.execute("INSERT INTO mart_line_stops VALUES "
                        "('88','outbound','weekday','A','Alpha_Street','Ashton',53.5,-2.2,1,0,40),"
                        "('88','outbound','*','A','Alpha_Street','Ashton',53.5,-2.2,1,0,90)")
        else:
            con.execute('CREATE TABLE mart_eta_model(line VARCHAR, direction VARCHAR, '
                        'hour_band VARCHAR, slope_s_per_km DOUBLE, intercept_s DOUBLE, '
                        'r2 DOUBLE, n_obs BIGINT)')
            con.execute("INSERT INTO mart_eta_model VALUES "
                        "('88','outbound','am_peak',150,90,0.7,400),"
                        "('*','*','*',170,70,0.5,9000)")
            con.execute('CREATE TABLE mart_line_stops(line VARCHAR, direction VARCHAR, '
                        'atco_code VARCHAR, stop_name VARCHAR, locality VARCHAR, '
                        'lat DOUBLE, lon DOUBLE, seq INTEGER, '
                        'median_secs_from_origin DOUBLE, n_obs BIGINT)')
            con.execute("INSERT INTO mart_line_stops VALUES "
                        "('88','outbound','A','Alpha_Street','Ashton',53.5,-2.2,1,0,40)")
        con.close()

    tmp = tempfile.mkdtemp()
    try:
        # --- new shape ---------------------------------------------------
        new_path = tmp + '/new.duckdb'
        build(new_path, with_day=True)
        marts = app._load_marts(new_path)
        assert marts['ok'] and marts['n_models'] == 3
        assert marts['day_types'] == ['*', 'sat', 'weekday'], marts['day_types']
        m = app.pick_model(marts['eta'], '88', 'outbound', 'weekday', 'am_peak')
        assert m['source'] == 'line+direction+day+hour_band' and m['n_obs'] == 400
        m = app.pick_model(marts['eta'], '88', 'outbound', 'sat', 'am_peak')
        assert m['source'] == 'line+direction+day' and m['n_obs'] == 300
        assert ('88', 'outbound', 'weekday') in marts['stops']
        assert app.stops_for(marts, '88', 'outbound', 'sun')[0]['atco'] == 'A'

        # --- old shape ---------------------------------------------------
        old_path = tmp + '/old.duckdb'
        build(old_path, with_day=False)
        marts = app._load_marts(old_path)
        # Loads rather than throwing and disabling predictions.
        assert marts['ok'] and marts['n_models'] == 2
        assert marts['day_types'] == ['*'], marts['day_types']
        # Every key is filed under '*', so the day rungs miss and the chain
        # resolves one grain coarser -- on every day of the week.
        for day in ('weekday', 'sat', 'sun'):
            m = app.pick_model(marts['eta'], '88', 'outbound', day, 'am_peak')
            assert m is not None and m['source'] == 'line+direction+hour_band', (day, m)
            assert app.stops_for(marts, '88', 'outbound', day)[0]['atco'] == 'A', day
            stop, _km = app.nearest_stop(marts, '88', 'outbound', day, 53.5001, -2.2001)
            assert stop is not None and stop['atco'] == 'A', day
        # Stop names are still de-underscored on the old shape.
        assert marts['stop_by_atco']['A']['name'] == 'Alpha Street'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_route_stops_resolver_matches_stops_for():
    """route_stops_for_line must resolve through the exact same key stops_for()
    would, or the map could draw a different sequence than the one the delay
    math is using -- the two would silently disagree about the route."""
    marts, a, m, b = _grid_marts()

    stops, direction, day, source = app.route_stops_for_line(marts, '88', 'outbound', 'weekday')
    assert stops == [a, m, b] and direction == 'outbound' and day == 'weekday'
    assert source == 'exact'
    assert stops == app.stops_for(marts, '88', 'outbound', 'weekday')

    # Day misses, falls back to the '*' row set the marts always emit... except
    # this fixture only has 'weekday', so it should fall through to the merge.
    stops2, _d, _day, source2 = app.route_stops_for_line(marts, '88', 'outbound', 'sun')
    assert source2 == 'line_merged_all_directions'
    assert stops2 == [a, m, b]   # same three stops, just not via a day-specific key

    # Opposite direction: the mart only knows 'outbound', request 'inbound'.
    stops3, direction3, _day, source3 = app.route_stops_for_line(marts, '88', 'inbound', 'weekday')
    assert direction3 == 'outbound'
    assert source3 in ('opposite_direction', 'opposite_direction_day_agnostic')
    assert stops3 == [a, m, b]

    # Unknown line entirely.
    stops4, _d, _day, source4 = app.route_stops_for_line(marts, 'NOPE', None, 'weekday')
    assert stops4 == [] and source4 == 'no_data'


def test_route_endpoint_validates_input():
    client = app.app.test_client()
    for bad in ("88; DROP TABLE bus_positions", "a" * 40, "line/with/slash"):
        r = client.get('/route', query_string={'line': bad})
        assert r.status_code == 400, bad
    # DIRECTION_RE is a character/length whitelist, not an enum -- it exists to
    # keep injection-shaped input out, the same discipline as bus_id elsewhere.
    # A real word like 'sideways' is syntactically fine and must NOT 400; only
    # something outside [A-Za-z_]{1,16} should.
    r = client.get('/route', query_string={'line': '88', 'direction': 'sideways'})
    assert r.status_code == 200
    r = client.get('/route', query_string={'line': '88', 'direction': 'north-west-ish!'})
    assert r.status_code == 400
    # day_type IS a closed enum (app.DAY_TYPES), unlike direction, because
    # the marts only ever emit these four literal values.
    r = client.get('/route', query_string={'line': '88', 'day_type': 'friday'})
    assert r.status_code == 400


def test_route_endpoint_no_data_degrades_quietly():
    client = app.app.test_client()
    r = client.get('/route', query_string={'line': 'NOSUCHLINE'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['features'] == []
    assert 'no stop sequence known' in body['properties']['note']


def test_route_truncation_and_merged_route_detection():
    """Both are measured directly against the module constants, so the test
    keeps holding even if the thresholds are retuned later."""
    marts = dict(app.EMPTY_MARTS)
    # A long, straight, well-behaved route: N stops 1 km apart. Must NOT be
    # flagged as merged even though it is long enough to be truncated.
    n = app.MAX_ROUTE_STOPS + 20
    good = [{'atco': 'S%d' % i, 'name': 'Stop %d' % i, 'locality': 'X',
             'lat': 53.0 + i * 0.009, 'lon': -2.2, 'seq': i,
             'secs_from_origin': float(i * 60)} for i in range(n)]
    marts['stops'] = {('LONG', 'outbound', 'weekday'): good}
    stops, _d, _day, _s = app.route_stops_for_line(marts, 'LONG', 'outbound', 'weekday')
    assert len(stops) == n   # resolver itself does not truncate

    usable = stops[:app.MAX_ROUTE_STOPS]
    assert len(usable) == app.MAX_ROUTE_STOPS
    gaps = [app.haversine_km(a['lat'], a['lon'], b['lat'], b['lon'])
            for a, b in zip(usable, usable[1:])]
    assert max(gaps) < app.MERGED_ROUTE_HOP_KM

    # Now splice in one implausible jump -- the merged-route signature.
    bad = list(good[:5])
    bad.append({'atco': 'FAR', 'name': 'Another Town Entirely', 'locality': 'Y',
                'lat': 54.5, 'lon': -3.5, 'seq': 5, 'secs_from_origin': 300.0})
    marts['stops'] = {('MERGED', 'outbound', 'weekday'): bad}
    stops2, _d, _day, _s = app.route_stops_for_line(marts, 'MERGED', 'outbound', 'weekday')
    flagged = any(app.haversine_km(a['lat'], a['lon'], b['lat'], b['lon']) > app.MERGED_ROUTE_HOP_KM
                  for a, b in zip(stops2, stops2[1:]))
    assert flagged


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn()
        print('ok   %s' % fn.__name__)
    print('\n%d tests passed' % len(tests))
