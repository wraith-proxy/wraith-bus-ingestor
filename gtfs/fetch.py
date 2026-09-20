#!/usr/bin/env python3
"""Weekly refresh of the GB-wide BODS GTFS timetable into a DuckDB file.

Downloads the public feed, loads it into a STAGING DuckDB, runs gtfs/patterns.sql
to derive the two serving tables (gtfs_route_stops, gtfs_line_localities), checks
the result is sane, and only then atomically renames the staging file over the
live one. A bad or half-vanished feed therefore never replaces a good one.

shapes.txt (2.57GB) is never extracted: OSRM already does geometry.

  fetch.py [--no-record]     --no-record skips the QuestDB run row (test runs)
"""
import contextlib
import csv
import fcntl
import logging
import os
import shutil
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

SOURCE_URL = 'https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/all/'
HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('GTFS_DB_PATH', '/mnt/archive/wraith-bus-ingestor-gtfs/gtfs.duckdb'))
WORK = Path(os.environ.get('GTFS_WORK_DIR', '/mnt/archive/wraith-bus-ingestor-gtfs/work'))
NAPTAN_CSV = os.environ.get(
    'NAPTAN_CSV', str(HERE.parent / 'dbt' / 'data' / 'naptan_stops.csv'))
# The host reaches QuestDB's HTTP port on 9001 (the container-network default,
# db:9000, does not resolve outside docker).
QUESTDB_URL = os.environ.get('QUESTDB_URL', 'http://localhost:9001')
LOG_FILE = HERE / 'refresh.log'

MIN_FREE_BYTES = 12 * 1024**3   # 5.3GB of CSVs + the DB + headroom
MIN_ZIP_BYTES = 500 * 1024**2   # the real feed is ~1.34GB; an HTML error page is KBs
# shapes.txt deliberately absent. feed_info.txt is the only optional member.
MEMBERS = ['agency', 'stops', 'routes', 'calendar', 'calendar_dates', 'trips',
           'stop_times', 'frequencies', 'feed_info']
# The auto-detector turns numeric-looking values into integers and breaks joins
# (*_id columns are forced separately) or aborts on the first 'X1' after 20480
# numeric rows. Text columns patterns.sql keys on are pinned. The dates arrive as
# VARCHAR 'YYYYMMDD'; patterns.sql casts them.
VARCHAR_COLS = {'stop_code', 'parent_station', 'platform_code', 'route_short_name',
                'route_long_name', 'trip_headsign', 'trip_short_name', 'stop_name',
                'agency_noc', 'agency_name', 'start_date', 'end_date', 'date'}
DOWNLOAD_DEADLINE_S = 45 * 60   # a trickle never trips the per-read timeout; this does

_log = logging.getLogger('gtfs')


def log(msg):
    _log.info(msg)


def stage(msg):
    log(f'==> {msg}')


def download(dest):
    req = urllib.request.Request(SOURCE_URL, headers={'User-Agent': 'wraith-bus-ingestor-gtfs/1'})
    # timeout is per socket read, so a stalled connection fails instead of hanging
    # forever while a slow-but-alive 1.3GB download is fine. 300s, not 60: BODS
    # assembles the zip on request and once took over 60s to send the first byte.
    deadline = time.time() + DOWNLOAD_DEADLINE_S
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, 'wb') as out:
        expected = resp.headers.get('Content-Length')
        while chunk := resp.read(1024 * 1024):
            out.write(chunk)
            if time.time() > deadline:
                raise RuntimeError(f'download exceeded {DOWNLOAD_DEADLINE_S // 60} min')
    size = dest.stat().st_size
    if expected is not None and size != int(expected):
        raise RuntimeError(f'download truncated: got {size} bytes, Content-Length {expected}')
    # ponytail: no testzip() here -- extract() CRC-checks every member it reads
    # and a BadZipFile there is fatal, so a corrupt body still cannot publish.
    if size < MIN_ZIP_BYTES or not zipfile.is_zipfile(dest):
        raise RuntimeError(f'download is not the feed: {size} bytes, valid zip={zipfile.is_zipfile(dest)}')
    log(f'downloaded {size / 1e9:.2f}GB')


def extract(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        have = set(zf.namelist())
        for name in MEMBERS:
            member = f'{name}.txt'
            if member not in have:
                if name == 'feed_info':
                    continue
                raise RuntimeError(f'{member} missing from the feed')
            with zf.open(member) as src, open(WORK / member, 'wb') as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            log(f'extracted {member}')


def read_csv_sql(name, extra_types=None, sample_all=True):
    """read_csv over one member with every *_id column forced to VARCHAR."""
    path = WORK / f'{name}.txt'
    with open(path, newline='', encoding='utf-8-sig') as f:
        header = next(csv.reader(f))
    types = {c: 'VARCHAR' for c in header if c.endswith('_id') or c in VARCHAR_COLS}
    types.update(extra_types or {})
    types = ', '.join(f"'{c}': '{t}'" for c, t in types.items() if c in header)
    types = f', types={{{types}}}' if types else ''   # feed_info.txt has no column to pin
    # sample_size=-1 for the small tables so one late 'X1' cannot flip a column's
    # type; stop_times (59M rows) keeps the default, its columns are all pinned.
    sample = ', sample_size=-1' if sample_all else ''
    return f"read_csv('{path}', header=true{types}{sample})"


def build_staging(con):
    for name in ['agency', 'routes', 'trips', 'stops', 'calendar', 'calendar_dates']:
        con.execute(f'CREATE TABLE gtfs_{name} AS SELECT * FROM {read_csv_sql(name)}')
    # Times stay VARCHAR: GTFS runs past 24:00:00 ("25:10:00" is 01:10 next day).
    src = read_csv_sql('stop_times', {'arrival_time': 'VARCHAR', 'departure_time': 'VARCHAR',
                                      'stop_sequence': 'INTEGER'}, sample_all=False)
    con.execute('CREATE TABLE gtfs_stop_times AS SELECT trip_id, stop_id, stop_sequence, '
                f'arrival_time, departure_time FROM {src}')
    # Exactly the options dbt/models/staging/naptan sources use, and every raw
    # column, so patterns.sql derives locality the way stg_naptan__stops does.
    con.execute(f"CREATE TABLE naptan_stops AS SELECT * FROM read_csv('{NAPTAN_CSV}', "
                'header=true, all_varchar=true, sample_size=-1)')
    for t in ['gtfs_agency', 'gtfs_routes', 'gtfs_trips', 'gtfs_stops', 'gtfs_calendar',
              'gtfs_calendar_dates', 'gtfs_stop_times', 'naptan_stops']:
        log(f'{t}: {count(con, t):,} rows')


def count(con, table):
    return con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]


def previous_counts():
    if not DB_PATH.exists():
        return {}
    try:
        with contextlib.closing(duckdb.connect(str(DB_PATH), read_only=True)) as old:
            return {t: count(old, t) for t in ('gtfs_route_stops', 'gtfs_line_localities')}
    except duckdb.Error as exc:
        log(f'previous DB unreadable, shrink check skipped: {exc!r}')
        return {}


def verify(con):
    n = {t: count(con, t) for t in ['gtfs_stop_times', 'gtfs_trips', 'gtfs_agency', 'gtfs_stops',
                                    'gtfs_route_stops', 'gtfs_line_localities']}
    # ponytail: these floors are estimates, not measured on the real feed (first run
    # should log the actual counts above; tighten them from those). route_stops is
    # roughly patterns x stops, well over 100k for GB.
    floors = {'gtfs_stop_times': 30_000_000, 'gtfs_trips': 800_000, 'gtfs_agency': 300,
              'gtfs_stops': 150_000, 'gtfs_route_stops': 100_000, 'gtfs_line_localities': 0}
    bad = [f'{t} has {n[t]:,} rows, need > {floor:,}' for t, floor in floors.items() if n[t] <= floor]
    for t, old in previous_counts().items():
        if old and n[t] < old * 0.7:
            bad.append(f'{t} shrank {old:,} -> {n[t]:,} (more than 30%)')
    # A patterns.sql bug (e.g. stop_id type mismatch) shows as NULL keys or broken seq.
    nulls = con.execute('SELECT count(*) FROM gtfs_route_stops WHERE operator IS NULL OR line IS NULL '
                        'OR direction IS NULL OR atco_code IS NULL').fetchone()[0]
    if nulls:
        bad.append(f'gtfs_route_stops has {nulls:,} rows with a NULL key column')
    badseq = con.execute('SELECT count(*) FROM (SELECT 1 FROM gtfs_route_stops GROUP BY operator, line, '
                         'direction HAVING min(seq) != 1 OR max(seq) != count(*) '
                         'OR count(DISTINCT seq) != count(*))').fetchone()[0]
    if badseq:
        bad.append(f'{badseq:,} (operator, line, direction) groups have a non-dense seq')
    if bad:
        raise RuntimeError('verification failed: ' + '; '.join(bad))
    return n


def write_meta(con, n):
    start = end = None
    if (WORK / 'feed_info.txt').exists():
        row = con.execute(
            "SELECT feed_start_date::VARCHAR, feed_end_date::VARCHAR "
            f"FROM {read_csv_sql('feed_info')} LIMIT 1").fetchone()
        start, end = row if row else (None, None)
    con.execute('CREATE TABLE gtfs_meta(loaded_at TIMESTAMP, source_url VARCHAR, '
                'feed_start_date VARCHAR, feed_end_date VARCHAR, n_stop_times BIGINT, '
                'n_trips BIGINT, n_route_stops BIGINT)')
    con.execute('INSERT INTO gtfs_meta VALUES (?, ?, ?, ?, ?, ?, ?)',
                [datetime.now(UTC).replace(tzinfo=None), SOURCE_URL, start, end,
                 n['gtfs_stop_times'], n['gtfs_trips'], n['gtfs_route_stops']])


def check_paths():
    """Refuse layouts where cleaning WORK could delete the DB (or the home dir)."""
    work, db = WORK.resolve(), DB_PATH.resolve()
    if work in (Path('/'), Path.home().resolve()) or len(work.parts) < 3:
        sys.exit(f'GTFS_WORK_DIR {work} is too broad to clean')
    if db == work or work in db.parents:
        sys.exit(f'GTFS_DB_PATH {db} is inside GTFS_WORK_DIR {work}; refusing')


def clean_work():
    """Delete only what this script creates, never the directory itself."""
    for p in [WORK / 'gtfs.zip', *WORK.glob('*.txt')]:
        p.unlink(missing_ok=True)
    if (WORK / 'spill').exists():
        shutil.rmtree(WORK / 'spill')


def refresh(staging):
    stage(f'checking free space on {WORK} and {DB_PATH.parent}')
    for d in (WORK, DB_PATH.parent):
        free = shutil.disk_usage(d).free
        log(f'{free / 1e9:.1f}GB free on {d}')
        if free < MIN_FREE_BYTES:
            raise RuntimeError(f'only {free / 1e9:.1f}GB free on {d}, need {MIN_FREE_BYTES / 1e9:.0f}GB')
    # A failed run leaves its CSVs, zip and spill behind; start clean so a stale
    # feed_info.txt or spill file cannot leak into this run.
    stage('cleaning leftovers from any earlier run')
    clean_work()

    zip_path = WORK / 'gtfs.zip'
    stage(f'downloading {SOURCE_URL}')
    download(zip_path)
    stage('extracting (never shapes.txt)')
    extract(zip_path)

    stage(f'building staging db {staging}')
    for p in (staging, Path(f'{staging}.wal')):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(staging))
    try:
        # memory_limit is not process RSS: expect roughly 4.7GB above it (unit caps at 8G).
        con.execute(f"SET memory_limit='4GB'; SET threads=3; SET preserve_insertion_order=false; "
                    f"SET temp_directory='{WORK / 'spill'}'")
        build_staging(con)
        stage('running gtfs/patterns.sql')
        patterns, trips, share = con.execute((HERE / 'patterns.sql').read_text()).fetchone()
        log(f'pattern stats: {patterns:,} distinct patterns over {trips:,} live trips; '
            f'primaries cover {share:.1%} of trips')
        for t in ['gtfs_route_stops', 'gtfs_line_localities']:
            log(f'{t}: {count(con, t):,} rows')
        stage('verifying')
        n = verify(con)
        write_meta(con, n)
        con.execute('CHECKPOINT')
    finally:
        con.close()

    stage(f'publishing {DB_PATH}')
    Path(f'{DB_PATH}.wal').unlink(missing_ok=True)   # stale wal of the file being replaced
    os.replace(staging, DB_PATH)
    try:
        clean_work()   # CSVs, zip and spill; the DB is already published, so never fail on this
    except OSError as exc:
        log(f'cleanup failed (will be retried at the next run): {exc!r}')
    return n


def record_run(status, n, started, message):
    """One row per run for Grafana. Mirrors archiver.record_run: a failure to
    record is logged and swallowed, never allowed to mask the real outcome."""
    try:
        message = message[:400].replace("'", "''")   # truncate first: never split a '' pair
        n = n or {}
        for q in (
            'CREATE TABLE IF NOT EXISTS gtfs_refresh_runs (run_at TIMESTAMP, status SYMBOL, '
            'n_stop_times LONG, n_trips LONG, n_route_stops LONG, duration_s DOUBLE, '
            'message VARCHAR) TIMESTAMP(run_at) PARTITION BY YEAR',
            f"INSERT INTO gtfs_refresh_runs VALUES(now(), '{status}', "
            f"{n.get('gtfs_stop_times', 0)}, {n.get('gtfs_trips', 0)}, "
            f"{n.get('gtfs_route_stops', 0)}, {time.time() - started:.1f}, '{message}')"):
            url = QUESTDB_URL + '/exec?' + urllib.parse.urlencode({'query': q})
            with urllib.request.urlopen(url, timeout=30) as resp:
                body = resp.read().decode('utf-8', 'replace')
            if '"error"' in body:   # QuestDB can answer 200 with an error body
                log(f'QuestDB rejected the run row: {body[:300]}')
    except Exception as exc:
        log(f'could not record run status: {exc!r}')


def main():
    args = sys.argv[1:]
    if args not in ([], ['--no-record']):
        sys.exit(__doc__)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE)])
    socket.setdefaulttimeout(300)
    # systemd's TimeoutStartSec sends SIGTERM; turn it into SystemExit so the
    # handler below cleans staging and records the 'error' row.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    check_paths()

    stage('taking lock')
    WORK.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)   # else connect fails after the download
    lock = open(WORK.parent / 'refresh.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f'another refresh holds {lock.name}; exiting')

    started = time.time()
    staging = DB_PATH.with_name(DB_PATH.name + '.staging')
    n = None
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        n = refresh(staging)
    except BaseException as exc:
        for p in (staging, Path(f'{staging}.wal')):
            p.unlink(missing_ok=True)
        log(f'FAILED: {exc!r}')
        if '--no-record' not in args:
            record_run('error', n, started, repr(exc))
        raise   # non-zero exit so systemd sees the failure
    elapsed = time.time() - started
    log(f'done in {elapsed / 60:.1f} min')
    if '--no-record' not in args:
        record_run('ok', n, started, '')


if __name__ == '__main__':
    main()
