"""Moves bus_positions partitions older than RETENTION_DAYS off the hot
QuestDB disk and onto cold storage, instead of deleting them.

Why this exists rather than QuestDB's native `SET TTL`: TTL drops partitions
outright. The user wants long-term storage on a separate drive, not deletion,
so the cutover here is DETACH PARTITION (QuestDB's own mechanism for lifting a
partition out of the live table without destroying it) followed by a plain
file copy to the archive volume, verified by size before the local copy is
freed. A partition that fails to copy is left detached-but-local rather than
half-migrated -- it costs disk space until the next run retries it, which is
a far better failure mode than losing a day of data.

Runs as root in its own container: the partition directories on disk are
owned by QuestDB's in-container uid (10001), which the host user cannot
write to, and matching that uid exactly is more fragile long-term than just
running this one narrowly-scoped maintenance container as root.
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
QDB_DB_ROOT = '/questdb_data/db'
ARCHIVE_ROOT = '/archive'
SENTINEL = os.path.join(ARCHIVE_ROOT, '.this-is-vault2')
POLL_S = 24 * 60 * 60

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


def qdb_exec(query):
    url = QUESTDB_URL + '/exec?' + urllib.parse.urlencode({'query': query})
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def attached_partitions():
    """[(name, min_timestamp), ...] for partitions currently in the live
    table, oldest first. Anything already `.detached` or otherwise not a
    plain YYYY-MM-DD day partition is skipped -- this table is PARTITION BY
    DAY, so a name that doesn't parse as a date is not ours to touch."""
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


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def table_dir():
    """Ask QuestDB where the table lives instead of globbing for a name.
    RENAME TABLE changes the SQL name but keeps the physical directory, so
    after docs/adr/0006's cutover the live bus_positions is bus_positions_v2~17
    on disk and a `bus_positions~*` glob finds nothing -- this run failed with
    exactly that on 2026-09-19. directoryName is authoritative whatever the
    table was originally called or renamed to."""
    rows = qdb_exec(
        f"SELECT directoryName FROM tables() WHERE table_name = '{TABLE}'"
    ).get('dataset', [])
    if len(rows) != 1:
        raise RuntimeError(f'expected exactly one table named {TABLE}, found {rows!r}')
    path = os.path.join(QDB_DB_ROOT, rows[0][0])
    if not os.path.isdir(path):
        raise RuntimeError(f'{TABLE} reports directory {rows[0][0]} but {path} is not a directory')
    return path


def orphaned_detached_names():
    """Partitions left as <name>.detached on a previous run that crashed, was
    killed, or lost power between the DETACH succeeding and the copy
    finishing. attached_partitions() will never see these again -- QuestDB
    already dropped them from the live table -- so a crash-resume path has
    to look at the filesystem directly, not just ask QuestDB what's live."""
    pattern = os.path.join(table_dir(), '*.detached')
    return sorted(os.path.basename(p)[:-len('.detached')] for p in glob.glob(pattern))


def copy_and_finalize(name):
    """<name>.detached already exists in the table dir -- copy it to the
    archive, verify by size, then free the local copy. Never deletes
    anything before the copy is confirmed byte-identical."""
    src = os.path.join(table_dir(), name + '.detached')
    if not os.path.isdir(src):
        log(f'ERROR: expected {src} to exist -- stopping this run')
        return False

    dest = os.path.join(ARCHIVE_ROOT, TABLE, name)
    if os.path.exists(dest):
        log(f'ERROR: archive destination {dest} already exists -- stopping, needs a human')
        return False

    src_bytes = dir_size(src)
    log(f'copying {src} ({src_bytes} bytes) -> {dest}')
    shutil.copytree(src, dest)
    dest_bytes = dir_size(dest)

    if dest_bytes != src_bytes:
        log(f'ERROR: size mismatch after copy ({src_bytes} local vs {dest_bytes} archived) -- '
            'leaving both copies in place, needs a human')
        return False

    shutil.rmtree(src)
    log(f'archived {name}: {src_bytes} bytes, freed from hot storage')
    return True


def archive_one(name):
    log(f'detaching partition {name}')
    resp = qdb_exec(f"ALTER TABLE {TABLE} DETACH PARTITION LIST '{name}'")
    if 'ddl' not in resp:
        log(f'DETACH failed for {name}: {resp!r} -- leaving it attached, will retry next run')
        return False

    # DETACH is sequenced through QuestDB's WAL and applied by a background
    # job -- "ddl":"OK" means the command was accepted, not that the
    # .detached directory has been renamed into place on disk yet. Give it a
    # few seconds before treating a missing directory as a real failure.
    src = os.path.join(table_dir(), name + '.detached')
    for _ in range(20):
        if os.path.isdir(src):
            break
        time.sleep(0.5)

    return copy_and_finalize(name)


def run_once():
    if not os.path.isfile(SENTINEL):
        log(f'ERROR: {SENTINEL} missing -- archive volume is not really mounted '
            '(or the sentinel got deleted). Refusing to touch any partitions '
            'this run rather than risk writing to the wrong disk.')
        return

    # Finish anything a previous, interrupted run only half-completed before
    # looking at what's newly eligible.
    for name in orphaned_detached_names():
        log(f'resuming interrupted archive of {name}')
        if not copy_and_finalize(name):
            return

    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    for name, min_ts_str in attached_partitions():
        min_ts = datetime.fromisoformat(min_ts_str.replace('Z', '+00:00'))
        if min_ts >= cutoff:
            break   # ordered oldest-first: nothing after this is old enough either
        if not archive_one(name):
            break   # stop on first failure rather than compounding it


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
            run_once()
        except Exception as exc:
            log(f'run failed: {exc!r}')
        # Event.wait (not time.sleep) so a signal during the 24h wait returns
        # immediately instead of only being noticed on the next wake.
        stop.wait(POLL_S)


if __name__ == '__main__':
    main()
