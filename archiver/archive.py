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
import os
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

QUESTDB_URL = os.environ.get('QUESTDB_URL', 'http://db:9000')
TABLE = 'bus_positions'
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '35'))
QDB_DB_ROOT = '/questdb_data/db'
ARCHIVE_ROOT = '/archive'
SENTINEL = os.path.join(ARCHIVE_ROOT, '.this-is-vault2')
POLL_S = 24 * 60 * 60


def log(msg):
    line = '%s %s' % (datetime.now(timezone.utc).isoformat(timespec='seconds'), msg)
    print(line, flush=True)
    try:
        with open(os.path.join(ARCHIVE_ROOT, 'archive.log'), 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass   # archive volume unreachable -- stdout still has it


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
        "SELECT name, minTimestamp FROM table_partitions('%s') "
        "WHERE attached = true ORDER BY minTimestamp" % TABLE)
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
    """bus_positions~N -- the suffix is assigned by QuestDB and is not
    something to hardcode; discover it instead of guessing it never
    changes. Directories only: a bare glob also matches QuestDB's
    bus_positions~N.lock file."""
    matches = [m for m in glob.glob(os.path.join(QDB_DB_ROOT, TABLE + '~*')) if os.path.isdir(m)]
    if len(matches) != 1:
        raise RuntimeError('expected exactly one %s~* dir, found %r' % (TABLE, matches))
    return matches[0]


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
        log('ERROR: expected %s to exist -- stopping this run' % src)
        return False

    dest = os.path.join(ARCHIVE_ROOT, TABLE, name)
    if os.path.exists(dest):
        log('ERROR: archive destination %s already exists -- stopping, needs a human' % dest)
        return False

    src_bytes = dir_size(src)
    log('copying %s (%d bytes) -> %s' % (src, src_bytes, dest))
    shutil.copytree(src, dest)
    dest_bytes = dir_size(dest)

    if dest_bytes != src_bytes:
        log('ERROR: size mismatch after copy (%d local vs %d archived) -- '
            'leaving both copies in place, needs a human' % (src_bytes, dest_bytes))
        return False

    shutil.rmtree(src)
    log('archived %s: %d bytes, freed from hot storage' % (name, src_bytes))
    return True


def archive_one(name):
    log('detaching partition %s' % name)
    resp = qdb_exec("ALTER TABLE %s DETACH PARTITION LIST '%s'" % (TABLE, name))
    if 'ddl' not in resp:
        log('DETACH failed for %s: %r -- leaving it attached, will retry next run' % (name, resp))
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
        log('ERROR: %s missing -- archive volume is not really mounted '
            '(or the sentinel got deleted). Refusing to touch any partitions '
            'this run rather than risk writing to the wrong disk.' % SENTINEL)
        return

    # Finish anything a previous, interrupted run only half-completed before
    # looking at what's newly eligible.
    for name in orphaned_detached_names():
        log('resuming interrupted archive of %s' % name)
        if not copy_and_finalize(name):
            return

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for name, min_ts_str in attached_partitions():
        min_ts = datetime.fromisoformat(min_ts_str.replace('Z', '+00:00'))
        if min_ts >= cutoff:
            break   # ordered oldest-first: nothing after this is old enough either
        if not archive_one(name):
            break   # stop on first failure rather than compounding it


if __name__ == '__main__':
    while True:
        try:
            run_once()
        except Exception as exc:
            log('run failed: %r' % exc)
        time.sleep(POLL_S)
