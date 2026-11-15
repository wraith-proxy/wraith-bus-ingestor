"""Expand/contract gate for ODCS contract changes.

check_change(base, head) compares two contract files and returns the policy
violations of going from base to head. sunset/backfill are customProperties
entries ({property, value}) on a schema property.
"""

from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatch
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

BACKFILLS = {"derive", "null-before", "replay"}


@dataclass(frozen=True)
class Violation:
    code: str
    message: str


def _columns(doc):
    """{(table, column): property dict}"""
    return {
        (obj["name"], p["name"]): p
        for obj in doc.get("schema", [])
        for p in obj.get("properties", [])
    }


def _prop(column, name):
    for cp in column.get("customProperties", []):
        if cp.get("property") == name:
            return cp.get("value")
    return None


def _as_date(value):
    return value if value is None or isinstance(value, date) else date.fromisoformat(str(value))


def _physical_names(doc):
    """{table: physicalName glob} for objects whose files are read by path."""
    return {
        obj["name"]: obj["physicalName"]
        for obj in doc.get("schema", [])
        if obj.get("physicalName")
    }


def _table_name(t, physical):
    """Contract table a FROM item reads: its name, or the object whose physicalName
    matches the file path of a read_parquet/read_csv/... table function."""
    if t.name:
        return t.name
    fn = t.this
    if isinstance(fn, exp.Expression) and fn.expressions:
        arg = fn.expressions[0]
        if isinstance(arg, exp.Literal) and arg.is_string:
            base = arg.name.rsplit("/", 1)[-1]
            for table, glob in physical.items():
                if fnmatch(base, glob) or base == glob:
                    return table
    return ""


def _select_reads(sel, table, column, physical=None):
    """True if this SELECT (not its subqueries) reads table.column."""
    physical = physical or {}

    def mine(node):
        return node.find_ancestor(exp.Select) is sel

    tables = {
        t.alias_or_name: _table_name(t, physical) for t in sel.find_all(exp.Table) if mine(t)
    }
    if table not in tables.values():
        return False
    named = any(
        c.name == column and (not c.table or tables.get(c.table) == table)
        for c in sel.find_all(exp.Column)
        if mine(c) and not isinstance(c.this, exp.Star)
    )
    return named or any(mine(star) for star in sel.find_all(exp.Star))


def _readers(sql_dir, table, column, physical=None):
    """Compiled SQL files that read table.column, by file stem.

    ponytail: per-SELECT name matching, not full lineage. A select over the table
    that mentions the column, or uses an unqualified/table-qualified star, counts as a
    reader. Over-reports across CTE/subquery name clashes; upgrade to sqlglot's
    optimizer.qualify if that bites.
    """
    return [
        path.stem
        for path in sorted(Path(sql_dir).rglob("*.sql"))
        if any(
            _select_reads(sel, table, column, physical)
            for tree in sqlglot.parse(path.read_text(), read="duckdb")
            if tree
            for sel in tree.find_all(exp.Select)
        )
    ]


def _major(doc):
    return int(str(doc.get("version", "0")).split(".")[0])


def check_change(
    base: Path, head: Path, *, today: date | None = None, compiled_sql_dir: Path | None = None
) -> list[Violation]:
    base_doc = yaml.safe_load(Path(base).read_text())
    head_doc = yaml.safe_load(Path(head).read_text())
    base_cols, head_cols = _columns(base_doc), _columns(head_doc)
    today = today or date.today()
    needs_major = _major(head_doc) <= _major(base_doc)
    violations = []
    for key, col in base_cols.items():
        name = f"{key[0]}.{key[1]}"
        if key not in head_cols:
            sunset = _as_date(_prop(col, "sunset"))
            if sunset is None:
                violations.append(
                    Violation("removal-without-sunset", f"{name} removed but declared no sunset")
                )
            elif today < sunset:
                violations.append(
                    Violation(
                        "removed-before-sunset", f"{name} removed before its sunset {sunset}"
                    )
                )
            if needs_major:
                violations.append(
                    Violation(
                        "breaking-without-major-bump",
                        f"{name} removed without a major version bump",
                    )
                )
            if compiled_sql_dir is not None and (
                readers := _readers(compiled_sql_dir, key[0], key[1], _physical_names(base_doc))
            ):
                violations.append(
                    Violation(
                        "removal-with-readers",
                        f"{name} removed but still read by {', '.join(readers)}",
                    )
                )
        elif head_cols[key].get("logicalType") != col.get("logicalType") and needs_major:
            violations.append(
                Violation(
                    "breaking-without-major-bump",
                    f"{name} changed type without a major version bump",
                )
            )
    for key, col in head_cols.items():
        if key in base_cols:
            continue
        name, backfill = f"{key[0]}.{key[1]}", _prop(col, "backfill")
        if backfill is None:
            violations.append(
                Violation("expand-without-backfill", f"{name} is new and declares no backfill")
            )
        elif backfill not in BACKFILLS:
            violations.append(
                Violation(
                    "invalid-backfill", f"{name} backfill {backfill!r} not in {sorted(BACKFILLS)}"
                )
            )
    return violations


def main(argv):
    base, head, *rest = argv
    violations = check_change(
        Path(base), Path(head), compiled_sql_dir=Path(rest[0]) if rest else None
    )
    for v in violations:
        print(f"{v.code}: {v.message}")
    return 1 if violations else 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
