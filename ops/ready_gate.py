"""Validate the extract window against its contract and write the readiness marker.

Ready gate: validate the whole extract window against its contract, then write
the readiness marker only when every error-severity check passes. Warning-severity
rules never block. A failed validation never writes the marker.

This module reads Parquet extracts with an in-memory DuckDB and writes only the
marker file. It must never touch a live QuestDB or the warehouse, and it must
never write the marker after a failed check.
"""

import operator
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

_OPS = {
    "mustBe": operator.eq,
    "mustNotBe": operator.ne,
    "mustBeGreaterThan": operator.gt,
    "mustBeGreaterOrEqualTo": operator.ge,
    "mustBeLessThan": operator.lt,
    "mustBeLessOrEqualTo": operator.le,
}


def _failures(contract: dict, data_dir: Path) -> list[str]:
    fails = []
    con = duckdb.connect()
    try:
        for obj in contract.get("schema", []):
            glob = str(Path(data_dir) / (obj.get("physicalName") or obj["name"]))
            try:
                con.execute(
                    f"CREATE VIEW o AS SELECT * FROM read_parquet('{glob}', union_by_name=true)"
                )
                cols = {r[0] for r in con.execute("DESCRIBE o").fetchall()}
            except Exception as exc:
                fails.append(f"{obj['name']}: unreadable: {str(exc).splitlines()[0][:160]}")
                continue
            for prop in obj.get("properties", []):
                name = prop.get("physicalName") or prop["name"]
                if name not in cols:
                    fails.append(f"{obj['name']}.{name}: column absent")
                elif prop.get("required") or prop.get("primaryKey"):
                    q = f'SELECT count(*) FROM o WHERE "{name}" IS NULL'
                    n = con.execute(q).fetchone()[0]
                    if n:
                        fails.append(f"{obj['name']}.{name}: {n} NULLs in a required column")
            for rule in obj.get("quality", []) or []:
                if rule.get("type") != "sql" or rule.get("severity", "error") != "error":
                    continue
                known = [k for k in _OPS if k in rule]
                if not known:
                    fails.append(f"{rule.get('id')}: no recognised operator, cannot evaluate")
                    continue
                for key in known:
                    value = con.execute(rule["query"].replace("{object}", "o")).fetchone()[0]
                    if not _OPS[key](value, rule[key]):
                        fails.append(f"{rule.get('id')}: {key} {rule[key]}, got {value}")
            con.execute("DROP VIEW o")
    finally:
        con.close()
    return fails


def validate(contract: Path, data_dir: Path, marker: Path) -> bool:
    """True and marker written when the extract meets the contract; else False, no marker.

    Args:
        contract: Path to the ODCS contract YAML.
        data_dir: Directory holding the extract Parquet files.
        marker: Path of the readiness marker to write on success.

    Returns:
        True if every error-severity check passed and the marker was written,
        False otherwise (nothing written).
    """
    doc = yaml.safe_load(Path(contract).read_text())
    fails = _failures(doc, Path(data_dir))
    for f in fails:
        print(f"ready_gate FAIL {f}", file=sys.stderr)
    if fails:
        return False
    marker = Path(marker)
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(datetime.now(UTC).isoformat() + "\n")
    os.replace(tmp, marker)
    return True


if __name__ == "__main__":
    c, d, m = (Path(a) for a in sys.argv[1:4])
    sys.exit(0 if validate(c, d, m) else 1)
