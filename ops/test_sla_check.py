"""SLA checker: breaches of a contract's slaProperties and the data-as-of stamp.
DuckDB runs in-memory or on tmp files; nothing here touches the network."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
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


# --- CLI: what dbt/build.sh calls instead of a bare `mv` --------------------------------

OPS = Path(__file__).resolve().parent
BUILD_SH = OPS.parent / "dbt" / "build.sh"


def make_extract(data_dir, newest):
    data_dir.mkdir()
    con = duckdb.connect()
    older, latest = (f"{t:%Y-%m-%d %H:%M:%S}" for t in (newest - timedelta(hours=2), newest))
    con.execute(
        f"copy (select * from (values (timestamp '{older}'), (timestamp '{latest}')) t(timestamp))"
        f" to '{data_dir}/bus_positions_2026-11-14_00.parquet' (format parquet)"
    )
    con.close()


def publish_cli(tmp_path, newest, version="3.1.1", sla_hours=26, unit="h"):
    contract = write_contract(
        tmp_path, [{"property": "latency", "value": sla_hours, "unit": unit}], version=version
    )
    data = tmp_path / "data"
    make_extract(data, newest)
    staged, served = tmp_path / "staged.duckdb", tmp_path / "served.duckdb"
    duckdb.connect(str(staged)).execute("create table eta_models as select 1 as x").close()
    served.write_bytes(b"old")
    r = subprocess.run(
        [sys.executable, str(OPS / "sla_check.py"), "publish", str(staged), str(served),
         "--data-dir", str(data), "--contract", str(contract)],
        capture_output=True, text=True,
    )
    return r, staged, served


def test_cli_publish_stamps_then_swaps(tmp_path):
    newest = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=1)
    r, staged, served = publish_cli(tmp_path, newest)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not staged.exists()
    assert sla_check.contract_version(served) == "3.1.1"
    assert sla_check.data_as_of(served).replace(tzinfo=None) == newest
    assert "SLA breach" not in r.stdout + r.stderr


def test_cli_publish_reports_a_breach_without_blocking(tmp_path):
    newest = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=40)
    r, staged, served = publish_cli(tmp_path, newest)
    assert r.returncode == 0, "an SLA breach is a ticket, not a failed publish"
    assert "SLA breach" in r.stdout + r.stderr
    assert not staged.exists() and sla_check.contract_version(served) == "3.1.1"


def test_cli_publish_with_no_extract_leaves_the_served_file_alone(tmp_path):
    contract = write_contract(tmp_path, [{"property": "latency", "value": 26, "unit": "h"}])
    staged, served = tmp_path / "staged.duckdb", tmp_path / "served.duckdb"
    duckdb.connect(str(staged)).execute("create table t as select 1 as x").close()
    served.write_bytes(b"old")
    r = subprocess.run(
        [sys.executable, str(OPS / "sla_check.py"), "publish", str(staged), str(served),
         "--data-dir", str(tmp_path / "missing"), "--contract", str(contract)],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert served.read_bytes() == b"old" and staged.exists()


def test_build_sh_publishes_through_the_sla_stamp():
    sh = BUILD_SH.read_text()
    assert "sla_check.py publish" in sh
    assert 'mv -f "$STAGED" "$SERVED"' not in sh, "a bare mv would skip the _meta stamp"
    assert sh.index("validate.py --marts") < sh.index("sla_check.py publish"), (
        "the stamp and swap come after the contract gate"
    )
    r = subprocess.run(["bash", "-n", str(BUILD_SH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cli_publish_survives_an_sla_check_that_cannot_run(tmp_path):
    newest = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=1)
    r, staged, served = publish_cli(tmp_path, newest, unit="fortnights")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SLA check could not run" in r.stdout + r.stderr
    assert not staged.exists() and sla_check.contract_version(served) == "3.1.1"
