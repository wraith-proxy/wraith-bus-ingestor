"""The generated bods sources keep what the hand-written yml carried (seam 1)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BODS = ROOT / "dbt" / "models" / "staging" / "bods"
SOURCES = BODS / "_wraith_bus_extract_positions__sources.yml"


def _source():
    doc = yaml.safe_load(SOURCES.read_text())
    (source,) = [s for s in doc["sources"] if s["name"] == "bods"]
    return source


def _table():
    (table,) = [t for t in _source()["tables"] if t["name"] == "bus_positions"]
    return table


def test_hand_written_sources_yml_is_gone():
    assert not (BODS / "_bods__sources.yml").exists()


def test_generated_sources_keep_external_location():
    assert _table()["meta"]["external_location"] == (
        "read_parquet('data/bus_positions_*.parquet', union_by_name=true)"
    )


def test_generated_sources_keep_freshness():
    # dbt inherits source-level freshness settings into every table
    s, t = _source(), _table()
    assert t.get("loaded_at_field", s.get("loaded_at_field")) == '"timestamp"'
    fresh = t.get("freshness", s.get("freshness"))
    assert fresh["warn_after"] == {"count": 2, "period": "day"}
    assert fresh["error_after"] == {"count": 5, "period": "day"}


def test_generated_sources_keep_column_set_test():
    tests = _table().get("data_tests", [])
    assert any("columns_match_set" in t for t in tests if isinstance(t, dict))
