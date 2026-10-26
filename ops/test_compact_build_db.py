"""ops/compact_build_db.sh against a throwaway warehouse: it must copy only the
three incremental tables (segmentation trimmed to the post-hook's 5 dates and
without shoelace_term, history whole), and leave the real file exactly as it
was whenever anything fails or it refuses."""
import os
import shutil
import subprocess
import sys

import duckdb
import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compact_build_db.sh")
SEG = "int_pings_segmented_to_journeys"

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0 or not (shutil.which("rsync") and shutil.which("fuser")),
    reason="needs rsync, fuser and a non-root user (the script refuses root)")


def make(tmp_path, history_dates=9):
    """Nine ping_local_dates of ten rows, the two history tables, two table
    models and a view: the real file's shape, on a fake vault2."""
    db = tmp_path / "dbt" / "wraith_bus_ingestor_build.duckdb"
    db.parent.mkdir()
    archive = tmp_path / "vault2"
    archive.mkdir()
    (archive / ".this-is-vault2").touch()
    con = duckdb.connect(str(db))
    con.execute(f"create table {SEG} as select date '2026-10-16' + (i // 10)::int as ping_local_date, "  # noqa: E501
                "i as ping_id, 0.5 as shoelace_term from range(90) t(i)")
    for t in ("int_eta_daily_stats", "int_journey_delays"):
        con.execute(f"create table {t} as select date '2026-10-16' + (i // 2)::int as stats_date, "
                    f"i as n from range({history_dates * 2}) t(i)")
    con.execute("create table eta_models as select i as n_obs from range(7) t(i)")
    con.execute("create table stg_things as select i from range(3) t(i)")
    con.execute(f"create view stg_bods__vehicle_pings as select * from {SEG}")
    con.close()
    return db, archive, tmp_path


@pytest.fixture
def warehouse(tmp_path):
    return make(tmp_path)


def run(warehouse, hhmm="1200", args=(), **env):
    db, archive, tmp = warehouse
    env = dict(os.environ, COMPACT_DB=str(db), COMPACT_ARCHIVE=str(archive),
               COMPACT_BACKUP_DIR=str(archive / "build_db_backup"),
               COMPACT_PY=sys.executable, COMPACT_LOGDIR=str(tmp / "logs"),
               COMPACT_UTC_HHMM=hhmm,
               COMPACT_PGREP_PATTERN='no-such-process-zz9', **env)
    rc = subprocess.run(["bash", SCRIPT, *args], env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log = tmp / "logs" / "compact.log"
    return rc.returncode, log.read_text() if log.exists() else rc.stdout


def snapshot(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return (
            con.execute(f"select count(*), min(ping_local_date)::varchar from {SEG}").fetchone(),
            [c for (c,) in con.execute(
                f"select column_name from duckdb_columns() where table_name = '{SEG}'").fetchall()],  # noqa: E501
            sorted(t for (t,) in con.execute("select table_name from duckdb_tables()").fetchall()),
            con.execute("select count(*), count(distinct stats_date) from int_eta_daily_stats").fetchone(),  # noqa: E501
            con.execute("select count(*), count(distinct stats_date) from int_journey_delays").fetchone(),  # noqa: E501
        )
    finally:
        con.close()


def test_keeps_three_tables_trimmed_without_shoelace_term(warehouse):
    db, archive, _ = warehouse
    before = snapshot(db)
    rc, log = run(warehouse)
    assert rc == 0, log
    # max 2026-10-24 minus 4 days: 10-20 .. 10-24, five dates of ten rows
    assert snapshot(db) == (
        (50, "2026-10-20"), ["ping_local_date", "ping_id"],
        ["int_eta_daily_stats", "int_journey_delays", SEG], (18, 9), (18, 9))
    (backup,) = (archive / "build_db_backup").iterdir()
    assert snapshot(backup) == before   # the rollback is the untouched original
    assert not os.path.exists(f"{db}.compact")
    assert "ROLLBACK needs BOTH" in log


def test_check_and_dry_run_write_nothing(warehouse):
    db, archive, _ = warehouse
    before, mtime = snapshot(db), db.stat().st_mtime_ns
    assert run(warehouse, args=["--check"])[0] == 0
    rc, log = run(warehouse, args=["--dry-run"])
    assert rc == 0, log
    assert snapshot(db) == before and db.stat().st_mtime_ns == mtime
    assert not (archive / "build_db_backup").exists()
    assert not os.path.exists(f"{db}.compact")


def test_a_row_count_mismatch_leaves_the_warehouse_untouched(warehouse):
    db, _, _ = warehouse
    before, mtime = snapshot(db), db.stat().st_mtime_ns
    rc, log = run(warehouse, COMPACT_TEST_FAIL="1")
    assert rc == 1, log
    assert f"FAIL {SEG}" in log
    assert snapshot(db) == before and db.stat().st_mtime_ns == mtime
    assert not os.path.exists(f"{db}.compact")


def test_a_short_backup_stops_before_the_swap(warehouse, tmp_path):
    db, _, _ = warehouse
    before, mtime = snapshot(db), db.stat().st_mtime_ns
    fake = tmp_path / "bin"
    fake.mkdir()
    rsync = fake / "rsync"   # copies, then truncates: a short backup
    rsync.write_text('#!/bin/bash\nsrc="${@: -2:1}"; dst="${@: -1}"; cp "$src" "$dst"; truncate -s 100 "$dst"\n')  # noqa: E501
    rsync.chmod(0o755)
    rc, log = run(warehouse, PATH=f"{fake}:{os.environ['PATH']}")
    assert rc == 1, log
    assert "the warehouse" in log
    assert snapshot(db) == before and db.stat().st_mtime_ns == mtime


def test_refuses_thin_history(tmp_path):
    w = make(tmp_path, history_dates=8)
    before = snapshot(w[0])
    rc, log = run(w)
    assert rc == 1, log
    assert "history too thin" in log
    assert snapshot(w[0]) == before
    assert not (w[1] / "build_db_backup").exists()


@pytest.mark.parametrize("hold", [False, True], ids=["build-window", "file-open"])
def test_refuses_before_touching_anything(warehouse, hold):
    db, archive, _ = warehouse
    con = duckdb.connect(str(db), read_only=True) if hold else None
    try:
        rc, log = run(warehouse, hhmm="1200" if hold else "0300")
    finally:
        if con:
            con.close()
    assert rc == 1, log
    assert ("has the warehouse open" if hold else "refusing inside 01:30-05:00") in log
    assert not (archive / "build_db_backup").exists()
