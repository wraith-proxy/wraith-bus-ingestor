"""Moves bus_positions days older than RETENTION_DAYS off the hot QuestDB
disk and onto cold storage as Parquet, instead of deleting them.

Why Parquet rather than QuestDB's own partition files (docs/adr/0007): a
DETACHed QuestDB partition is not self-contained. Its SYMBOL columns are
integer keys into the source table's symbol maps, which are not archived with
it, and QuestDB re-attaches a partition only into the table it came from --
so dropping or recreating that table strands the whole archive (63 days were
lost that way, docs/adr/0006). A Parquet file carries real strings and its own
schema: it can be read by DuckDB straight off the archive drive, or loaded
back into any table.

Why not QuestDB's native `SET TTL`: TTL drops partitions outright. This
exports the day first, proves the file, and only then drops the live copy.
The order matters -- a day is never dropped until its Parquet file has been
read back and matches QuestDB row for row.

Each run also records itself in QuestDB (`archiver_runs`), which is what the
Grafana alert rules read: Grafana can see QuestDB, not this drive.
"""
import glob
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

QUESTDB_URL = os.environ.get('QUESTDB_URL', 'http://db:9000')
TABLE = 'bus_positions'
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '22'))
ARCHIVE_ROOT = '/archive'
PARQUET_DIR = os.path.join(ARCHIVE_ROOT, 'bus_positions_parquet')
SENTINEL = os.path.join(ARCHIVE_ROOT, '.this-is-vault2')
POLL_S = 24 * 60 * 60
# zstd level 3 took a 34M-row day to 0.69GB in 25s; level 9 saved a further
# 3.6% for 2.3x the time (measured 2026-09-19 against the real table), and the
# QuestDB default (lz4) was 1.22GB. Raw partition: ~3.0GB.
PARQUET_CODEC = 'zstd'
PARQUET_LEVEL = 3
EXPORT_TIMEOUT_S = 900
# Files written to the archive drive belong to the host user, not root, so the
# owner can manage them without sudo. This container runs as root only so it
# can do exactly that.
OWNER = tuple(int(x) for x in os.environ.get('ARCHIVE_OWNER', '1000:1000').split(':'))

# logging.raiseExceptions=False mirrors the old bare `except OSError: pass` on
# the file write -- if the archive volume drops out from under the
# FileHandler mid-run, stdout must keep getting every line rather than the
# process taking a logging error to stderr on every call.
logging.raiseExceptions = False
_LOG_FMT = logging.Formatter('%(asctime)sZ %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
_LOG_FMT.converter = time.gmtime

_logger = logging.getLogger('archiver')
_logger.setLevel(logging.INFO)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_LOG_FMT)
_logger.addHandler(_stream_handler)
try:
    _file_handler = logging.FileHandler(os.path.join(ARCHIVE_ROOT, 'archive.log'))
    _file_handler.setFormatter(_LOG_FMT)
    _logger.addHandler(_file_handler)
except OSError:
    pass   # archive volume unreachable at startup -- stdout still has it


def log(msg):
    _logger.info(msg)


def qdb_exec(query, timeout=30):
    url = QUESTDB_URL + '/exec?' + urllib.parse.urlencode({'query': query})
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def attached_partitions():
    """[(name, min_timestamp), ...] for partitions currently in the live
    table, oldest first. A name that doesn't parse as a date is not ours to
    touch -- this table is PARTITION BY DAY."""
    result = qdb_exec(
        f"SELECT name, minTimestamp FROM table_partitions('{TABLE}') "
        "WHERE attached = true ORDER BY minTimestamp")
    out = []
    for name, min_ts in result.get('dataset', []):
        try:
            datetime.strptime(name, '%Y-%m-%d')
        except ValueError:
            continue
        out.append((name, min_ts))
    return out


def live_rows(name):
    return qdb_exec(
        f"SELECT count() FROM {TABLE} WHERE timestamp IN '{name}'")['dataset'][0][0]


def export_day(name, dest):
    """Stream QuestDB's own Parquet export of one day into `dest`. Streaming:
    the archiver's memory does not grow with the day (curl peaked at 11MB on a
    34M-row day), and QuestDB peaked at ~4GB of its 8GB doing the same."""
    params = {
        'query': f"SELECT * FROM {TABLE} WHERE timestamp IN '{name}'",
        'fmt': 'parquet',
        'compression_codec': PARQUET_CODEC,
        'compression_level': PARQUET_LEVEL,
    }
    url = QUESTDB_URL + '/exp?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=EXPORT_TIMEOUT_S) as resp, open(dest, 'wb') as out:
        shutil.copyfileobj(resp, out, 1 << 20)


def parquet_stats(path):
    """(rows, min_timestamp, max_timestamp) read back out of the file itself --
    the proof, as opposed to trusting that the export worked. Timestamps come
    back as text: QuestDB writes TIMESTAMPTZ, and fetching one as a Python
    datetime needs pytz, which this slim image does not carry (found the hard
    way on the first real run, 2026-09-19)."""
    import duckdb
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")   # so the date prefix is the UTC day
        return con.execute(
            'SELECT count(*), min(timestamp)::VARCHAR, max(timestamp)::VARCHAR '
            'FROM read_parquet(?)',
            [path]).fetchone()
    finally:
        con.close()


def drop_partition(name):
    resp = qdb_exec(f"ALTER TABLE {TABLE} DROP PARTITION LIST '{name}'")
    if 'ddl' not in resp:
        log(f'ERROR: DROP PARTITION failed for {name}: {resp!r}')
        return False
    return True


def archive_one(name):
    """'archived' | 'skipped' | 'failed'. 'skipped' means a day left in place
    that a human should look at, without blocking the days behind it; 'failed'
    stops the run."""
    dest = os.path.join(PARQUET_DIR, name + '.parquet')
    expected = live_rows(name)

    if os.path.exists(dest):
        # Already archived: a day `ops/incident.py restore` brought back, or a
        # run interrupted between the rename and the drop. Never rewrite it.
        # Drop the live copy only if the file matches it exactly -- a mismatch
        # is not something to resolve by guessing which side is right.
        rows = parquet_stats(dest)[0]
        if rows == expected:
            if not drop_partition(name):
                return 'failed'
            log(f'{name}: already archived ({rows:,} rows verified), dropped the live copy')
            return 'archived'
        log(f'WARNING: {name}: parquet has {rows:,} rows but the live table has {expected:,} '
            '-- skipping, left the live copy in place to check by hand')
        return 'skipped'

    # Written as .part and renamed only once proven, so a crash can never leave
    # a half-written file looking like a finished archive.
    part = dest + '.part'
    log(f'exporting {name} ({expected:,} rows) -> {dest}')
    export_day(name, part)
    rows, lo, hi = parquet_stats(part)
    in_day = expected == 0 or (lo[:10] == name and hi[:10] == name)
    if rows != expected or not in_day:
        os.remove(part)
        log(f'ERROR: {name}: exported file failed verification ({rows:,} rows vs {expected:,} '
            f'expected, range {lo}..{hi}) -- discarded it, left the live copy in place')
        return 'failed'
    os.replace(part, dest)
    os.chown(dest, *OWNER)
    if not drop_partition(name):
        return 'failed'
    log(f'archived {name}: {rows:,} rows, {os.path.getsize(dest)} bytes, dropped from hot storage')
    return 'archived'


def run_once():
    """(status, archived, skipped, detail). status: 'ok' | 'attention' | 'error'."""
    if not os.path.isfile(SENTINEL):
        detail = (f'{SENTINEL} missing -- archive volume is not really mounted (or the sentinel '
                  'got deleted). Refusing to touch any partitions this run rather than risk '
                  'writing to the wrong disk.')
        log(f'ERROR: {detail}')
        return 'error', 0, 0, detail

    os.makedirs(PARQUET_DIR, exist_ok=True)
    os.chown(PARQUET_DIR, *OWNER)
    for stale in glob.glob(os.path.join(PARQUET_DIR, '*.part')):
        os.remove(stale)   # an export interrupted mid-write

    archived = skipped = 0
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    for name, min_ts_str in attached_partitions():
        min_ts = datetime.fromisoformat(min_ts_str.replace('Z', '+00:00'))
        if min_ts >= cutoff:
            break   # ordered oldest-first: nothing after this is old enough either
        result = archive_one(name)
        if result == 'archived':
            archived += 1
        elif result == 'skipped':
            skipped += 1
        else:
            return 'error', archived, skipped, f'{name}: archive failed, see archive.log'
    if skipped:
        return 'attention', archived, skipped, f'{skipped} day(s) left in place, see archive.log'
    return 'ok', archived, skipped, ''


STATUS_DDL = (
    'CREATE TABLE IF NOT EXISTS archiver_runs '
    '(ts TIMESTAMP, status SYMBOL, archived LONG, skipped LONG, detail VARCHAR) '
    'TIMESTAMP(ts) PARTITION BY MONTH')


def record_run(status, archived, skipped, detail):
    """One row per run: what the Grafana rules read. A failure to record must
    never take the archiver down -- and if QuestDB is unreachable the missing
    row is itself the signal (the 'no clean run' rule fires)."""
    try:
        detail = detail.replace("'", "''")[:400]
        qdb_exec(STATUS_DDL)
        qdb_exec(
            "INSERT INTO archiver_runs VALUES"
            f"(now(), '{status}', {archived}, {skipped}, '{detail}')")
    except Exception as exc:
        log(f'could not record run status: {exc!r}')


def main():
    stop = threading.Event()

    def handle_signal(signum, frame):
        stop.set()

    # So `docker stop` (SIGTERM) exits cleanly within the grace period
    # instead of blocking through the 24h wait and getting SIGKILLed.
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not stop.is_set():
        try:
            result = run_once()
        except Exception as exc:
            log(f'run failed: {exc!r}')
            result = ('error', 0, 0, repr(exc))
        record_run(*result)
        # Event.wait (not time.sleep) so a signal during the 24h wait returns
        # immediately instead of only being noticed on the next wake.
        stop.wait(POLL_S)


if __name__ == '__main__':
    main()
