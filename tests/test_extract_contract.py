"""The extract-Parquet contract must describe exactly what dbt/extract.py writes."""

import ast
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "bus-positions-extract.odcs.yaml"
EXTRACT = ROOT / "dbt" / "extract.py"


def _extract_constants():
    """KEYS/WANTED/TYPES read via ast; extract.py is never imported or run."""
    wanted = {"KEYS", "WANTED", "TYPES"}
    found = {}
    for node in ast.parse(EXTRACT.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in wanted:
                found[node.targets[0].id] = ast.literal_eval(node.value)
    return found


def _contract():
    return yaml.safe_load(CONTRACT.read_text())


def _props():
    schema = _contract()["schema"]
    assert len(schema) == 1
    return {p["name"]: p for p in schema[0]["properties"]}


def test_extract_contract_lists_every_column_extract_writes():
    c = _extract_constants()
    expected = set(c["KEYS"]) | set(c["WANTED"])
    props = _props()
    assert set(props) == expected
    for name, prop in props.items():
        assert prop["physicalType"] == c["TYPES"][name], name
        assert prop.get("logicalType"), name
        assert prop.get("description"), name


def test_timestamp_documented_as_bucket_end():
    desc = _props()["timestamp"]["description"].lower()
    assert "end of" in desc and "3-minute" in desc and "bucket" in desc
    assert "not ingest time" in desc


def test_published_line_and_block_ref_absent_from_extract_contract():
    props = _props()
    assert "published_line" not in props
    assert "block_ref" not in props


def test_custom_properties_is_array_of_property_value():
    cp = _contract()["customProperties"]
    assert isinstance(cp, list) and cp
    for entry in cp:
        assert {"property", "value"} <= set(entry)
    names = {e["property"] for e in cp}
    assert {"externalLocation", "freshnessWarnAfter", "freshnessErrorAfter",
            "loadedAtField"} <= names


def test_extract_contract_lints_clean():
    binary = shutil.which("datacontract")
    assert binary is not None, "datacontract-cli not on PATH"
    result = subprocess.run(
        [binary, "lint", str(CONTRACT)], capture_output=True, text=True, timeout=120
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "warning" not in output.lower(), output
