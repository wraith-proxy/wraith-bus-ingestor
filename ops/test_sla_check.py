"""SLA checker: breaches of a contract's slaProperties and the data-as-of stamp.
DuckDB runs in-memory or on tmp files; nothing here touches the network."""

from datetime import UTC, datetime, timedelta

import sla_check
import yaml

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def write_contract(tmp_path, sla, version="1.2.0"):
    path = tmp_path / "c.odcs.yaml"
    path.write_text(yaml.safe_dump({"name": "c", "version": version, "slaProperties": sla}))
    return path


def test_breach_when_latency_exceeded(tmp_path):
    contract = write_contract(tmp_path, [{"property": "latency", "value": 26, "unit": "h"}])
    breaches = sla_check.check_slas(contract, NOW, as_of=NOW - timedelta(hours=30))
    assert [b.property for b in breaches] == ["latency"]
    assert "30" in breaches[0].message and "26" in breaches[0].message


def test_no_breach_within_latency(tmp_path):
    contract = write_contract(tmp_path, [{"property": "latency", "value": 26, "unit": "h"}])
    assert sla_check.check_slas(contract, NOW, as_of=NOW - timedelta(hours=25)) == []


def test_latency_unit_minutes(tmp_path):
    contract = write_contract(tmp_path, [{"property": "latency", "value": 10, "unit": "m"}])
    assert sla_check.check_slas(contract, NOW, as_of=NOW - timedelta(minutes=9)) == []
    assert len(sla_check.check_slas(contract, NOW, as_of=NOW - timedelta(minutes=11))) == 1


def test_meta_round_trip(tmp_path):
    db = tmp_path / "staged.duckdb"
    sla_check.write_meta(db, NOW, "1.2.0")
    assert sla_check.data_as_of(db) == NOW
    assert sla_check.contract_version(db) == "1.2.0"


def make_db(path, rows=1):
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("create table t as select range as n from range(?)", [rows])
    con.close()


def test_meta_table_written_before_publish(tmp_path):
    served, staged = tmp_path / "served.duckdb", tmp_path / "staged.duckdb"
    make_db(served, rows=1)
    make_db(staged, rows=2)
    sla_check.publish(staged, served, NOW, "1.2.0")
    assert not staged.exists()
    assert sla_check.data_as_of(served) == NOW  # the stamp travelled with the swap


def test_failed_stamp_leaves_served_file_untouched(tmp_path):
    import duckdb
    import pytest

    served, staged = tmp_path / "served.duckdb", tmp_path / "staged.duckdb"
    make_db(served)
    before = served.read_bytes()
    staged.write_text("not a duckdb file")
    with pytest.raises(duckdb.Error):
        sla_check.publish(staged, served, NOW, "1.2.0")
    assert served.read_bytes() == before


def test_public_signatures_typed_and_documented():
    import inspect

    for fn in (
        sla_check.check_slas,
        sla_check.data_as_of,
        sla_check.contract_version,
        sla_check.write_meta,
        sla_check.publish,
    ):
        assert inspect.getdoc(fn), f"{fn.__name__} has no docstring"
        sig = inspect.signature(fn)
        assert sig.return_annotation is not inspect.Signature.empty, fn.__name__
        for p in sig.parameters.values():
            assert p.annotation is not inspect.Parameter.empty, f"{fn.__name__}.{p.name}"
