"""The salvage must trim only real padding and refuse anything it does not understand."""
import duckdb
import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")
import salvage_legacy_archive as salvage  # noqa: E402

DAY = "2026-08-31"
START = int(np.datetime64(DAY, "us").astype("int64"))


def make_partition(tmp_path, ts, pad=3, txn_rows=None):
    d = tmp_path / DAY
    d.mkdir()
    n = len(ts)
    zeros = lambda dt: np.zeros(pad, dt)  # noqa: E731
    np.concatenate([np.array(ts, "<i8"), zeros("<i8")]).tofile(d / "timestamp.d")
    for name, base in (("lat", 53.0), ("lon", -2.0), ("bearing", 90.0)):
        np.concatenate([np.full(n, base, "<f8"), zeros("<f8")]).tofile(d / f"{name}.d")
    np.concatenate([np.arange(n, dtype="<i4"), zeros("<i4")]).tofile(d / "bus_id.d")
    np.concatenate([np.full(n, 7, "<i4"), zeros("<i4")]).tofile(d / "line.d.9")  # versioned name
    np.concatenate([np.full(n, -1, "<i4"), zeros("<i4")]).tofile(d / "vehicle_id.d.8")
    np.array([7777, txn_rows or n, 8888], "<i8").tofile(d / "_txn")  # no accidental match
    return d


def test_trims_tail_padding_and_keeps_only_what_survived(tmp_path):
    d = make_partition(tmp_path, [START + 10, START + 20, START + 30])
    out = tmp_path / "out.parquet"
    assert salvage.salvage_day(str(d), str(out)) == 3
    con = duckdb.connect()
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()]
    # vehicle_id (100% NULL) is not carried over
    assert cols == ["timestamp", "lat", "lon", "bearing", "bus_id_key", "line_key"]
    got = con.execute(f"SELECT count(*), sum(line_key), max(bus_id_key) FROM '{out}'").fetchone()
    assert got == (3, 21, 2)
    meta = con.execute(f"SELECT key, value FROM parquet_kv_metadata('{out}')").fetchall()
    assert (b"wraith.layout", b"numeric-salvage") in [(k, v) for k, v in meta]


def test_refuses_a_zero_in_the_middle(tmp_path):
    d = make_partition(tmp_path, [START + 10, 0, START + 30])
    with pytest.raises(AssertionError, match="tail padding"):
        salvage.salvage_day(str(d), str(tmp_path / "out.parquet"))


def test_refuses_rows_outside_the_day(tmp_path):
    d = make_partition(tmp_path, [START + 10, START + salvage.US_PER_DAY + 5])
    with pytest.raises(AssertionError, match="outside the day"):
        salvage.salvage_day(str(d), str(tmp_path / "out.parquet"))


def test_refuses_when_questdbs_own_row_count_disagrees(tmp_path):
    d = make_partition(tmp_path, [START + 10, START + 20], txn_rows=999)
    with pytest.raises(AssertionError, match="_txn"):
        salvage.salvage_day(str(d), str(tmp_path / "out.parquet"))
