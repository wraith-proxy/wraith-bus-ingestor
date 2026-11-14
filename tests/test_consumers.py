"""CDP/FDP contracts: one schema object per model, and every `consumes` declaration
must resolve to a real column of an upstream contract.

Contracts are addressed by file stem (bus-cdp, bus-fdp, bus-positions-extract, ...)
in `consumes` values: "<contract>.<table>.<column>". dbt is never run here.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = {
    p.name.removesuffix(".odcs.yaml"): yaml.safe_load(p.read_text())
    for p in sorted((ROOT / "contracts").glob("*.odcs.yaml"))
}
MODELS = ROOT / "dbt" / "models"


def _columns():
    """{contract: {table: {column, ...}}}"""
    return {
        stem: {
            obj["name"]: {p["name"] for p in obj.get("properties", [])} for obj in doc["schema"]
        }
        for stem, doc in CONTRACTS.items()
    }


def _consumes(stem):
    out = []
    for obj in CONTRACTS[stem]["schema"]:
        for entry in obj.get("customProperties", []):
            if entry["property"] == "consumes":
                out.append((obj["name"], entry["value"]))
    return out


def _model_names(layer):
    return {p.stem for p in (MODELS / layer).rglob("*.sql")}


def test_cdp_and_fdp_contracts_exist():
    assert {"bus-cdp", "bus-fdp"} <= set(CONTRACTS)


def test_consumes_is_array_of_property_value():
    for stem in ("bus-cdp", "bus-fdp"):
        for obj in CONTRACTS[stem]["schema"]:
            cp = obj.get("customProperties")
            assert isinstance(cp, list), (stem, obj["name"])
            for entry in cp:
                assert isinstance(entry, dict), (stem, obj["name"])
                assert {"property", "value"} <= set(entry), (stem, obj["name"])
    assert _consumes("bus-cdp") and _consumes("bus-fdp")


def test_every_consumed_column_exists_upstream():
    cols = _columns()
    checked = 0
    for stem in CONTRACTS:
        for table, value in _consumes(stem):
            parts = value.split(".")
            assert len(parts) == 3, (stem, table, value)
            contract, up_table, column = parts
            assert contract in cols, (stem, table, value)
            assert up_table in cols[contract], (stem, table, value)
            assert column in cols[contract][up_table], (stem, table, value)
            checked += 1
    assert checked > 0


@pytest.mark.parametrize("stem,layer", [("bus-cdp", "staging"), ("bus-fdp", "intermediate")])
def test_every_cdp_and_fdp_table_has_a_schema_object_per_model(stem, layer):
    names = [obj["name"] for obj in CONTRACTS[stem]["schema"]]
    assert len(names) == len(set(names))
    assert set(names) == _model_names(layer)
