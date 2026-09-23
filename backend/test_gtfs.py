"""Tests for the GTFS reader against a tiny hand-built database."""

import os
import tempfile

import duckdb

import gtfs


def _build(path):
    con = duckdb.connect(path)
    con.execute('CREATE TABLE naptan_stops(ATCOCode VARCHAR, CommonName VARCHAR, '
                'LocalityName VARCHAR, Latitude VARCHAR, Longitude VARCHAR, Status VARCHAR)')
    con.execute("INSERT INTO naptan_stops VALUES "
                "('A','Alpha_Street','Ashton','53.5','-2.2','inactive'),"
                "('A','Alpha Street','Ashton','53.6','-2.3','active'),"
                "('B','Beta Road','Bury','53.7','-2.4','active'),"
                "('Z','No Coords','Nowhere','','','active')")
    con.execute('CREATE TABLE gtfs_route_patterns(operator VARCHAR, line VARCHAR, '
                'direction VARCHAR, pattern_id INTEGER, is_primary BOOLEAN, n_trips INTEGER, '
                'n_stops INTEGER, first_atco VARCHAR, last_atco VARCHAR, '
                'is_circular BOOLEAN, trip_share DOUBLE)')
    con.execute("INSERT INTO gtfs_route_patterns VALUES "
                "('OP1','88','outbound',1,true,20,3,'A','C',false,0.8),"
                "('OP1','88','outbound',2,false,5,3,'A','C',false,0.2),"
                "('OP1','88','inbound',1,true,25,3,'C','A',false,1.0),"
                "('OP2','88','outbound',1,true,90,2,'X','Y',false,1.0)")
    con.execute('CREATE TABLE gtfs_route_stops(operator VARCHAR, line VARCHAR, '
                'direction VARCHAR, atco_code VARCHAR, stop_name VARCHAR, '
                'locality VARCHAR, lat DOUBLE, lon DOUBLE, '
                'seq INTEGER, sched_secs_from_origin DOUBLE, n_trips INTEGER, pattern_id INTEGER, '
                'is_primary BOOLEAN)')
    rows = []
    for pid, mid in ((1, 'M1'), (2, 'M2')):
        for seq, (atco, secs) in enumerate((('A', 0), (mid, 300), ('C', 600)), 1):
            rows.append(f"('OP1','88','outbound','{atco}','n{atco}','L',53.0,-2.0,"
                        f"{seq},{secs},5,{pid},{pid == 1})")
    for seq, (atco, secs) in enumerate((('C', 0), ('M1', 300), ('A', 600)), 1):
        rows.append(f"('OP1','88','inbound','{atco}','n{atco}','L',53.0,-2.0,"
                    f"{seq},{secs},5,1,true)")
    rows.append("('OP2','88','outbound','X','nX','L',54.0,-1.0,1,0,5,1,true)")
    rows.append("('OP2','88','outbound','Y','nY','L',54.1,-1.1,2,60,5,1,true)")
    con.execute('INSERT INTO gtfs_route_stops VALUES ' + ','.join(rows))
    con.execute('CREATE TABLE gtfs_line_alias(live_operator VARCHAR, live_line VARCHAR, '
                'gtfs_operator VARCHAR, gtfs_line VARCHAR, n_buses INTEGER, '
                'share DOUBLE, source VARCHAR)')
    con.execute("INSERT INTO gtfs_line_alias VALUES ('OP1','cot','OP1','88',3,1.0,'learned')")
    con.execute('CREATE TABLE gtfs_line_localities(line VARCHAR, locality_norm VARCHAR, '
                'locality VARCHAR, n_stops INTEGER)')
    con.execute("INSERT INTO gtfs_line_localities VALUES "
                "('88','ASHTON','Ashton',2),('9','ASHTON','Ashton',1)")

    # Raw GTFS tables for trip pinning (gtfs.pin_trip, ROADMAP.md 1.13.0).
    # Real column set is wider than this (see gtfs/fetch.py); only the
    # columns pin_trip's own queries touch are needed here.
    con.execute('CREATE TABLE gtfs_agency(agency_id VARCHAR, agency_noc VARCHAR)')
    con.execute("INSERT INTO gtfs_agency VALUES ('AG1','OP1')")
    con.execute('CREATE TABLE gtfs_routes(route_id VARCHAR, agency_id VARCHAR, '
                'route_short_name VARCHAR)')
    con.execute("INSERT INTO gtfs_routes VALUES ('R1','AG1','88')")
    con.execute('CREATE TABLE gtfs_calendar(service_id VARCHAR, monday INTEGER, '
                'tuesday INTEGER, wednesday INTEGER, thursday INTEGER, friday INTEGER, '
                'saturday INTEGER, sunday INTEGER, start_date VARCHAR, end_date VARCHAR)')
    con.execute("INSERT INTO gtfs_calendar VALUES "
                "('WD',1,1,1,1,1,0,0,'20240101','20241231'),"        # weekdays only
                "('SUN',0,0,0,0,0,0,1,'20240101','20241231')")       # Sundays only
    con.execute('CREATE TABLE gtfs_calendar_dates(service_id VARCHAR, date VARCHAR, '
                'exception_type INTEGER)')
    con.execute('CREATE TABLE gtfs_trips(route_id VARCHAR, service_id VARCHAR, '
                'trip_id VARCHAR)')
    con.execute("INSERT INTO gtfs_trips VALUES "
                "('R1','WD','T1'),"     # Mon-Fri 08:00 from A, unambiguous
                "('R1','SUN','T3'),"    # Sundays only, otherwise identical to T1
                "('R1','SUN','T4')")    # Sunday-into-Monday, past-midnight departure
    con.execute('CREATE TABLE gtfs_stop_times(trip_id VARCHAR, stop_id VARCHAR, '
                'stop_sequence INTEGER, arrival_time VARCHAR, departure_time VARCHAR)')
    con.execute("INSERT INTO gtfs_stop_times VALUES "
                "('T1','A',1,'08:00:00','08:00:00'),"
                "('T1','M1',2,'08:05:00','08:05:00'),"
                "('T1','C',3,'08:10:00','08:10:00'),"
                "('T3','A',1,'08:00:00','08:00:00'),"
                "('T3','C',2,'08:10:00','08:10:00'),"
                "('T4','A',1,'25:00:00','25:00:00'),"   # 01:00 the next calendar day
                "('T4','C',2,'25:10:00','25:10:00')")
    con.close()


def _add_ambiguous_trip(path):
    """T2: same operator/line/origin/departure as T1 (variant A/C has no
    destination in the key, ADR-0008), so adding it makes that key ambiguous."""
    con = duckdb.connect(path)
    con.execute("INSERT INTO gtfs_trips VALUES ('R1','WD','T2')")
    con.execute("INSERT INTO gtfs_stop_times VALUES "
                "('T2','A',1,'08:00:00','08:00:00'),('T2','B',2,'08:20:00','08:20:00')")
    con.close()


def _point_at(path):
    gtfs.GTFS_PATH = path
    gtfs._state.update(mtime=None, checked=0.0)
    gtfs._clear()


def test_missing_database_degrades_quietly():
    _point_at('/nonexistent/gtfs.duckdb')
    assert not gtfs.available()
    assert gtfs.stop('A') is None
    assert gtfs.candidates('OP1', '88', 'A', 'C') == []
    assert gtfs.line_route('OP1', '88') is None
    assert gtfs.line_localities(['ASHTON']) == {}


def test_stop_prefers_active_row_and_caches_misses():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        gtfs.prime(['A', 'B', 'Z', 'nope'])
        s = gtfs.stop('A')
        assert (s['lat'], s['lon'], s['name']) == (53.6, -2.3, 'Alpha Street')
        assert gtfs.stop('B')['locality'] == 'Bury'
        assert gtfs.stop('Z') is None and 'Z' in gtfs._stops      # no coords: known miss
        assert gtfs.stop('nope') is None and 'nope' in gtfs._stops
        assert gtfs.stop('') is None


def test_candidates_are_ordered_and_alias_resolved():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        c = gtfs.candidates('OP1', '88', 'A', 'C')
        assert [p['pattern_id'] for p in c] == [1, 2]                # trip_share desc
        assert [s['atco'] for s in c[0]['stops']] == ['A', 'M1', 'C']
        assert c[0]['stops'][1]['secs_from_origin'] == 300 and c[0]['stops'][1]['seq'] == 2
        assert gtfs.candidates('OP1', '88', 'A', 'NOPE') == []      # endpoints match nothing
        assert gtfs.candidates('OP1', '88', None, 'C') == []
        # A live label the timetable spells differently resolves through the alias.
        assert [p['pattern_id'] for p in gtfs.candidates('OP1', 'cot', 'A', 'C')] == [1, 2]
        assert gtfs.candidates('OP9', '88', 'A', 'C') == []          # unknown operator


def test_line_route_picks_primary_by_direction_then_trips():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        r = gtfs.line_route('OP1', '88', 'inbound')
        assert r['direction'] == 'inbound' and r['source'] == 'exact'
        assert [s['atco'] for s in r['stops']] == ['C', 'M1', 'A']
        r = gtfs.line_route('OP1', '88')                              # no direction: outbound
        assert r['direction'] == 'outbound' and r['stops'][1]['atco'] == 'M1'
        assert gtfs.line_route('OP1', '88', 'clockwise')['source'] == 'opposite_direction'
        assert gtfs.line_route(None, '88')['operator'] == 'OP2'       # most trips wins
        assert gtfs.line_route('OP9', '88') is None


def test_line_localities_and_cache_reset_on_new_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        assert gtfs.line_localities(['ASHTON', 'NOWHERE']) == {'ASHTON': {'88', '9'}}
        assert gtfs.stop('B') is not None
        # A refresh swaps the file: mtime moves, so every cache is dropped.
        os.utime(path, (1, 1))
        gtfs._state['checked'] = 0.0
        gtfs._check()
        assert gtfs._stops == {} and gtfs._cands == {}


def test_prime_routes_batches_and_survives_hostile_values():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        weird = "x'; DROP TABLE gtfs_route_stops; --\x1e\x1f"
        gtfs.prime_routes([('OP1', '88', 'A', 'C'), ('OP2', '88', 'X', 'Y'),
                           ('OP1', weird, 'A', 'C'), ('OP1', '88', None, 'C')])
        assert [p['pattern_id'] for p in gtfs._cands[('OP1', '88', 'A', 'C')]] == [1, 2]
        assert len(gtfs._cands[('OP2', '88', 'X', 'Y')][0]['stops']) == 2
        assert gtfs._cands[('OP1', weird, 'A', 'C')] == []
        assert ('OP1', '88', None, 'C') not in gtfs._cands       # incomplete key skipped
        gtfs.prime([weird, 'A'])
        assert gtfs.stop('A') is not None and gtfs.stop(weird) is None
        assert gtfs.candidates('OP1', '88', 'A', 'C')             # table still there


def test_prime_lines_batches_like_line_route():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        weird = "x'; DROP TABLE gtfs_route_stops; --\x1e\x1f"
        gtfs.prime_lines([('OP1', '88', 'inbound'), ('OP1', '88', None),
                          ('OP1', 'cot', 'outbound'), ('OP1', '88', 'clockwise'),
                          ('OP9', '88', None), ('OP1', weird, None), (None, '88', None)])
        assert gtfs._lines[('OP1', '88', 'inbound')]['direction'] == 'inbound'
        assert gtfs._lines[('OP1', '88', None)]['direction'] == 'outbound'
        assert gtfs._lines[('OP1', 'cot', 'outbound')]['stops'][1]['atco'] == 'M1'   # via alias
        assert gtfs._lines[('OP1', '88', 'clockwise')]['source'] == 'opposite_direction'
        assert gtfs._lines[('OP9', '88', None)] is None
        assert gtfs._lines[('OP1', weird, None)] is None
        assert (None, '88', None) not in gtfs._lines               # no operator: not batched
        assert gtfs.line_route('OP1', '88', 'inbound') is gtfs._lines[('OP1', '88', 'inbound')]
        assert gtfs.line_route(None, '88')['operator'] == 'OP2'      # single-key path still works


# Monday 2024-03-04 08:00 and 01:00 Europe/London (GMT, no BST -- no DST
# ambiguity to worry about). Real epoch-millis timestamps, same shape as
# row['origin_aimed_dep']. Picking real dates rather than "today" keeps the
# test deterministic regardless of when it runs -- pin_trip only cares about
# the calendar date the given timestamp falls on, never the real clock.
MON_0800 = 1709539200000
MON_0100 = 1709514000000    # lands under Sunday's service via the >24h GTFS convention


def test_pin_trip_unambiguous_match():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        pin = gtfs.pin_trip('OP1', '88', 'A', MON_0800)
        assert pin['trip_id'] == 'T1'
        assert [s['atco'] for s in pin['stops']] == ['A', 'M1', 'C']
        # anchor_epoch_s + a stop's own arr_secs is that stop's real epoch.
        c = next(s for s in pin['stops'] if s['atco'] == 'C')
        assert pin['anchor_epoch_s'] + c['arr_secs'] == MON_0800 / 1000.0 + 600  # 08:10


def test_pin_trip_ambiguous_and_inactive_service_do_not_pin():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        assert gtfs.pin_trip('OP1', '88', 'A', MON_0800)['trip_id'] == 'T1'
        gtfs._clear()
        _add_ambiguous_trip(path)
        gtfs._clear()
        # T1 and T2 now both run weekdays, same operator/line/origin/departure
        # -- variant A/C deliberately has no destination in the key
        # (ADR-0008), so this is a real ambiguity, not a bug.
        assert gtfs.pin_trip('OP1', '88', 'A', MON_0800) is None
        # T3 (Sunday-only service) proves an inactive-that-day service is
        # excluded outright, not merely counted as ambiguous: it never
        # touches the Monday pin above even though it shares every other
        # field with T1.
        assert gtfs.pin_trip('OP1', '88', 'A', MON_0800 + 6 * 86400 * 1000)['trip_id'] == 'T3'


def test_pin_trip_past_midnight_anchors_to_the_previous_service_day():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        # T4 is filed under Sunday's service_id with departure_time '25:00:00'
        # (past midnight); MON_0100 is the real wall-clock instant that
        # represents. T3 (plain Sunday 08:00) must not interfere.
        pin = gtfs.pin_trip('OP1', '88', 'A', MON_0100)
        assert pin['trip_id'] == 'T4'
        c = next(s for s in pin['stops'] if s['atco'] == 'C')
        # 25:10:00 on T4 is 00:10 the next real day -- 10 minutes after MON_0100.
        assert pin['anchor_epoch_s'] + c['arr_secs'] == MON_0100 / 1000.0 + 600


def test_pin_trip_no_match_and_missing_field_return_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        assert gtfs.pin_trip('OP1', '88', 'nowhere', MON_0800) is None
        assert gtfs.pin_trip('OP1', '88', 'A', None) is None
        assert gtfs.pin_trip(None, '88', 'A', MON_0800) is None


def test_prime_pins_batches_and_caches():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'gtfs.duckdb')
        _build(path)
        _point_at(path)
        keys = [('OP1', '88', 'A', MON_0800), ('OP1', '88', 'nowhere', MON_0800),
               ('OP1', '88', 'A', None)]
        gtfs.prime_pins(keys)
        assert gtfs._pins[('OP1', '88', 'A', MON_0800)]['trip_id'] == 'T1'
        assert gtfs._pins[('OP1', '88', 'nowhere', MON_0800)] is None
        assert ('OP1', '88', 'A', None) not in gtfs._pins    # incomplete key skipped
        assert gtfs.pin_trip('OP1', '88', 'A', MON_0800) is gtfs._pins[('OP1', '88', 'A', MON_0800)]
