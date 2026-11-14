"""Ready gate: validate the whole extract window against its contract and write
the readiness marker only when valid. Real DuckDB over tmp Parquet files; no network."""

import duckdb
import pytest
import ready_gate
import yaml

CONTRACT = {
    "name": "c",
    "schema": [
        {
            "name": "bus_positions",
            "physicalName": "bus_positions_*.parquet",
            "quality": [
                {
                    "id": "rows",
                    "type": "sql",
                    "severity": "error",
                    "query": "SELECT count(*) FROM {object}",
                    "mustBeGreaterOrEqualTo": 2,
                },
                {
                    "id": "lat",
                    "type": "sql",
                    "severity": "warning",
                    "query": "SELECT count(*) FROM {object} WHERE lat > 61",
                    "mustBe": 0,
                },
            ],
            "properties": [
                {"name": "bus_id", "required": True},
                {"name": "lat"},
            ],
        }
    ],
}


def write_extract(tmp_path, days):
    """days: {day: [(bus_id, lat), ...]} -> one parquet file per day."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    con = duckdb.connect()
    for day, rows in days.items():
        con.execute("CREATE OR REPLACE TABLE t(bus_id VARCHAR, lat DOUBLE)")
        for r in rows:
            con.execute("INSERT INTO t VALUES (?, ?)", list(r))
        con.execute(f"COPY t TO '{data}/bus_positions_{day}_00.parquet' (FORMAT PARQUET)")
    con.close()
    return data


@pytest.fixture
def contract(tmp_path):
    path = tmp_path / "c.odcs.yaml"
    path.write_text(yaml.safe_dump(CONTRACT))
    return path


def test_valid_extract_writes_marker(tmp_path, contract):
    data = write_extract(tmp_path, {"2026-11-12": [("a", 50.0)], "2026-11-13": [("b", 51.0)]})
    marker = tmp_path / "extract.ready"
    assert ready_gate.validate(contract, data, marker) is True
    assert marker.exists()


def test_invalid_extract_does_not_write_marker(tmp_path, contract):
    # one row fails the error-severity row-count rule
    data = write_extract(tmp_path, {"2026-11-13": [("a", 50.0)]})
    marker = tmp_path / "extract.ready"
    assert ready_gate.validate(contract, data, marker) is False
    assert not marker.exists()


def test_invalid_window_day_blocks_marker(tmp_path, contract):
    # whole window is checked: a null required column in an older day fails it
    data = write_extract(tmp_path, {"2026-11-12": [(None, 50.0)], "2026-11-13": [("b", 51.0)]})
    marker = tmp_path / "extract.ready"
    assert ready_gate.validate(contract, data, marker) is False
    assert not marker.exists()


def test_warning_rule_does_not_block(tmp_path, contract):
    data = write_extract(tmp_path, {"2026-11-12": [("a", 99.0)], "2026-11-13": [("b", 51.0)]})
    marker = tmp_path / "extract.ready"
    assert ready_gate.validate(contract, data, marker) is True


def test_failed_validation_leaves_no_stale_marker_write(tmp_path, contract):
    data = tmp_path / "data"
    data.mkdir()
    marker = tmp_path / "extract.ready"
    assert ready_gate.validate(contract, data, marker) is False
    assert not marker.exists()
