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
