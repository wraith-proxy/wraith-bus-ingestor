"""dbt/extract.py's .done markers (ADR-0010). A UTC day is fetched once and
then skipped for its marker, so a marker written over a short or half-written
day freezes that day for good. These lock the rules that keep that from
happening: a marked day sends no query, a day that dies midway or comes back
empty or is fetched while QuestDB's WAL lags stays unmarked, an empty re-fetch
never deletes a chunk that was already there, and prune takes old markers
with their chunks. QuestDB, duckdb and HTTP are all faked; nothing here
touches the network."""
import io
import json
import os
import re
import sys
from datetime import date
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "dbt"))

import extract  # noqa: E402


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 25)


class FakeCon:
    """Just enough duckdb connection for main() and fetch(): COPY writes a
    stand-in file at its TO path, count(*) answers `rows`."""

    def __init__(self, rows=0):
        self.rows = rows
        self.copied_to = []

    def execute(self, sql, params=None):
        m = re.search(r"TO '(.+?)' \(FORMAT", sql)
        if sql.startswith("COPY") and m:
            self.copied_to.append(m.group(1))
            with open(m.group(1), "wb") as f:
                f.write(b"fresh")
        return self

    def fetchone(self):
        return (self.rows,)

    def close(self):
        pass


def chunk(out, day, hour=0, body=b"rows"):
    path = os.path.join(out, f"bus_positions_{day}_{hour:02d}.parquet")
    with open(path, "wb") as f:
        f.write(body)
    return path


def mark(out, day):
    open(os.path.join(out, f"bus_positions_{day}.done"), "w").close()


def markers(out):
    return sorted(f for f in os.listdir(out) if f.endswith(".done"))


@pytest.fixture
def run(monkeypatch, tmp_path):
    """main() over a 3-day window (2026-10-22 .. 10-24) with every outside
    call faked. Returns a recorder of fetches and restarts."""
    out = str(tmp_path)
    rec = SimpleNamespace(out=out, fetches=[], restarts=0, empty_days=set(), fail_at=None)

    def fake_fetch(con, day, hour_start, hour_end, select, columns):
        if (day.isoformat(), hour_start) == rec.fail_at:
            raise RuntimeError("questdb went away mid-day")
        rec.fetches.append(day.isoformat())
        if day.isoformat() in rec.empty_days:
            return None, 0
        return chunk(out, day.isoformat(), hour_start), 10

    def fake_restart():
        rec.restarts += 1

    monkeypatch.setattr(extract, "OUT", out)
    monkeypatch.setattr(extract, "DAYS", 3)
    monkeypatch.setattr(extract, "date", FixedDate)
    monkeypatch.setattr(extract, "RESTART_QUESTDB_BETWEEN_DAYS", True)
    monkeypatch.setattr(extract, "questdb_columns", lambda: set(extract.WANTED))
    monkeypatch.setattr(extract, "fetch", fake_fetch)
    monkeypatch.setattr(extract, "restart_questdb", fake_restart)
    monkeypatch.setattr(extract, "wal_applied", lambda: (True, None))
    monkeypatch.setitem(sys.modules, "duckdb", SimpleNamespace(connect=lambda *a, **k: FakeCon()))
    return rec


def test_marked_day_is_skipped_before_any_fetch_and_restart(run):
    chunk(run.out, "2026-10-23")
    mark(run.out, "2026-10-23")

    extract.main()

    assert "2026-10-23" not in run.fetches
    assert set(run.fetches) == {"2026-10-22", "2026-10-24"}
    # One restart, between the two days actually fetched -- not one per window day.
    assert run.restarts == 1
    assert markers(run.out) == [f"bus_positions_2026-10-{d}.done" for d in (22, 23, 24)]


def test_partial_day_leaves_no_marker(run):
    run.fail_at = ("2026-10-22", 4)

    with pytest.raises(RuntimeError):
        extract.main()

    assert markers(run.out) == []


def test_wal_not_applied_leaves_no_marker(run, monkeypatch, capsys):
    monkeypatch.setattr(
        extract, "wal_applied",
        lambda: (False, "bus_positions WAL not fully applied (writerTxn 7, sequencerTxn 9)"))

    extract.main()

    assert markers(run.out) == []
    assert os.path.exists(os.path.join(run.out, "bus_positions_2026-10-24_00.parquet"))
    assert "not marked done" in capsys.readouterr().out


def test_empty_day_leaves_no_marker_and_the_window_still_passes(run):
    for d in ("2026-10-22", "2026-10-23"):
        chunk(run.out, d)
        mark(run.out, d)
    run.empty_days = {"2026-10-24"}

    extract.main()

    assert run.fetches.count("2026-10-24") == 24 // extract.CHUNK_HOURS
    assert "bus_positions_2026-10-24.done" not in markers(run.out)


def test_no_parquet_left_in_the_window_fails(run):
    chunk(run.out, "2026-10-21")  # before the window: does not count
    run.empty_days = {"2026-10-22", "2026-10-23", "2026-10-24"}

    with pytest.raises(AssertionError):
        extract.main()


def test_empty_refetch_never_deletes_an_existing_chunk(monkeypatch, tmp_path):
    monkeypatch.setattr(extract, "OUT", str(tmp_path))
    good = chunk(str(tmp_path), "2026-10-24", 0, body=b"good")
    con = FakeCon(rows=0)

    assert extract.fetch(con, date(2026, 10, 24), 0, 4, "*", "{}") == (None, 0)

    assert con.copied_to == [good + ".tmp"]
    with open(good, "rb") as f:
        assert f.read() == b"good"
    assert not os.path.exists(good + ".tmp")


def test_fetch_with_rows_replaces_the_chunk(monkeypatch, tmp_path):
    monkeypatch.setattr(extract, "OUT", str(tmp_path))
    good = chunk(str(tmp_path), "2026-10-24", 0, body=b"good")

    assert extract.fetch(FakeCon(rows=5), date(2026, 10, 24), 0, 4, "*", "{}") == (good, 5)

    with open(good, "rb") as f:
        assert f.read() == b"fresh"
    assert not os.path.exists(good + ".tmp")


def test_prune_removes_markers_before_the_window(monkeypatch, tmp_path):
    out = str(tmp_path)
    monkeypatch.setattr(extract, "OUT", out)
    for d in ("2026-10-16", "2026-10-17"):
        chunk(out, d)
        mark(out, d)

    removed = extract.prune_outside_window(date(2026, 10, 17))

    assert removed == ["bus_positions_2026-10-16.done", "bus_positions_2026-10-16_00.parquet"]
    assert sorted(os.listdir(out)) == ["bus_positions_2026-10-17.done",
                                       "bus_positions_2026-10-17_00.parquet"]


@pytest.mark.parametrize("answer, applied", [
    ({"dataset": [[False, 9, 9]]}, True),
    ({"dataset": [[False, 7, 9]]}, False),   # apply job behind the sequencer
    ({"dataset": [[True, 9, 9]]}, False),    # suspended
    ({"dataset": []}, False),                # table not listed
    (OSError("connection refused"), False),  # could not ask at all
])
def test_wal_applied_reads_wal_tables(monkeypatch, answer, applied):
    seen = {}

    def fake_urlopen(url, timeout=None):
        seen["url"] = url
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(extract.urllib.request, "urlopen", fake_urlopen)
    ok, why = extract.wal_applied()

    assert ok is applied
    assert (why is None) is applied
    assert "wal_tables" in seen["url"]
