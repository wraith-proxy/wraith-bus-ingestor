"""The archiver must never drop a day until its Parquet file is proven, and
must never let one odd day block the ones behind it."""
import os

import archive
import duckdb
import pytest


def write_parquet(path, day, n, at="12:00:00"):
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT TIMESTAMPTZ '{day} {at}+00' + to_seconds(i) AS timestamp, "
        f"'b' || i AS bus_id FROM range({n}) t(i)) TO '{path}' (FORMAT PARQUET)"
    )
    con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    pq = tmp_path / "pq"
    pq.mkdir()
    calls = {"dropped": [], "exported": []}
    live = {"2026-09-08": 5}

    def fake_qdb(query, timeout=30):
        if query.startswith("SELECT count()"):
            day = query.split("'")[1]
            return {"dataset": [[live[day]]]}
        if "DROP PARTITION" in query:
            calls["dropped"].append(query.split("'")[1])
            return {"ddl": "OK"}
        raise AssertionError(query)

    monkeypatch.setattr(archive, "qdb_exec", fake_qdb)
    monkeypatch.setattr(archive, "PARQUET_DIR", str(pq))
    monkeypatch.setattr(archive, "OWNER", (os.getuid(), os.getgid()))

    def exporter(rows, day="2026-09-08"):
        def export(name, dest):
            calls["exported"].append(name)
            write_parquet(dest, day, rows)
        monkeypatch.setattr(archive, "export_day", export)

    return pq, calls, live, exporter


def test_exports_verifies_then_drops(env):
    pq, calls, _, exporter = env
    exporter(5)
    assert archive.archive_one("2026-09-08") == "archived"
    assert (pq / "2026-09-08.parquet").exists()
    assert not list(pq.glob("*.part"))
    assert calls["dropped"] == ["2026-09-08"]


def test_row_mismatch_is_discarded_and_never_dropped(env):
    pq, calls, _, exporter = env
    exporter(4)  # QuestDB says 5
    assert archive.archive_one("2026-09-08") == "failed"
    assert not list(pq.iterdir())
    assert calls["dropped"] == []


def test_export_from_the_wrong_day_is_rejected(env):
    pq, calls, _, exporter = env
    exporter(5, day="2026-09-09")
    assert archive.archive_one("2026-09-08") == "failed"
    assert calls["dropped"] == []


def test_existing_matching_file_only_drops_the_live_copy(env):
    pq, calls, _, exporter = env
    write_parquet(pq / "2026-09-08.parquet", "2026-09-08", 5)
    exporter(5)
    assert archive.archive_one("2026-09-08") == "archived"
    assert calls["exported"] == []  # never re-exported over an existing archive
    assert calls["dropped"] == ["2026-09-08"]


def test_existing_mismatched_file_is_skipped_untouched(env):
    pq, calls, _, exporter = env
    write_parquet(pq / "2026-09-08.parquet", "2026-09-08", 4)
    before = (pq / "2026-09-08.parquet").read_bytes()
    assert archive.archive_one("2026-09-08") == "skipped"
    assert (pq / "2026-09-08.parquet").read_bytes() == before
    assert calls["dropped"] == []


def test_one_skipped_day_does_not_block_the_next(env, monkeypatch, tmp_path):
    pq, calls, live, exporter = env
    live["2026-09-07"] = 5
    write_parquet(pq / "2026-09-07.parquet", "2026-09-07", 4)  # mismatched -> skipped
    exporter(5)
    sentinel = tmp_path / ".sentinel"
    sentinel.touch()
    monkeypatch.setattr(archive, "SENTINEL", str(sentinel))
    monkeypatch.setattr(archive, "RETENTION_DAYS", 1)  # both days now older than the cutoff
    monkeypatch.setattr(archive, "attached_partitions", lambda: [
        ("2026-09-07", "2026-09-07T00:00:00.000000Z"),
        ("2026-09-08", "2026-09-08T00:00:00.000000Z"),
    ])
    status, archived, skipped, _ = archive.run_once()
    assert (status, archived, skipped) == ("attention", 1, 1)
    assert calls["dropped"] == ["2026-09-08"]


def test_missing_sentinel_touches_nothing(env, monkeypatch, tmp_path):
    _, calls, _, _ = env
    monkeypatch.setattr(archive, "SENTINEL", str(tmp_path / "absent"))
    assert archive.run_once()[0] == "error"
    assert calls["dropped"] == []
