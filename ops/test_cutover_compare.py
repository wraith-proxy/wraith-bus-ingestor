"""cutover_compare: keys and counts exact, DOUBLE columns within tolerance.
Throwaway DuckDB files only; no live warehouse."""

import cutover_compare as cc
import duckdb


def make(path, rows):
    con = duckdb.connect(str(path))
    con.execute("create table mart (line varchar, hr integer, avg_delay double)")
    for r in rows:
        con.execute("insert into mart values (?, ?, ?)", r)
    con.close()
    return str(path)


BASE = [("a", 1, 1.5), ("a", 2, 2.5), ("b", 1, 100.0)]


def test_identical_passes(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", BASE)
    assert cc.compare(a, b, ["mart"]).ok


def test_seeded_key_difference_fails(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", BASE[:2] + [("c", 1, 100.0)])
    r = cc.compare(a, b, ["mart"])
    assert not r.ok
    assert any("key" in p for p in r.problems)


def test_seeded_double_1e9_difference_passes(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", [(k, h, d + 1e-9) for k, h, d in BASE])
    assert cc.compare(a, b, ["mart"]).ok


def test_large_double_difference_fails(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", [("a", 1, 1.6)] + BASE[1:])
    r = cc.compare(a, b, ["mart"])
    assert not r.ok and any("double" in p for p in r.problems)


def test_row_count_difference_fails(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", BASE + BASE[:1])
    r = cc.compare(a, b, ["mart"])
    assert not r.ok and any("count" in p for p in r.problems)


def test_null_vs_value_double_fails(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", [("a", 1, None)] + BASE[1:])
    assert not cc.compare(a, b, ["mart"]).ok


def test_missing_table_fails(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", BASE)
    assert not cc.compare(a, b, ["nope"]).ok


def test_cli_exit_codes(tmp_path):
    a = make(tmp_path / "a.duckdb", BASE)
    b = make(tmp_path / "b.duckdb", BASE[:2])
    assert cc.main([a, a, "mart"]) == 0
    assert cc.main([a, b, "mart"]) == 1
