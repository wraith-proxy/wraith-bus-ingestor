"""Expand/contract gate: behaviour of check_change(base, head) on two contract files.

dbt is never run here. Reader detection (seam 2) works on injected compiled SQL.
"""

import importlib.util
from datetime import date
from pathlib import Path

import yaml

_SPEC = importlib.util.spec_from_file_location(
    "policy", Path(__file__).resolve().parent.parent / "contracts" / "policy.py"
)
policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy)


def contract(columns, version="1.0.0", table="t"):
    """columns: {name: {"type": ..., "props": [(property, value), ...]}} or name -> type str."""
    props = []
    for name, spec in columns.items():
        spec = {"type": spec} if isinstance(spec, str) else spec
        p = {"name": name, "logicalType": spec.get("type", "string")}
        if spec.get("props"):
            p["customProperties"] = [{"property": k, "value": v} for k, v in spec["props"]]
        props.append(p)
    return {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "name": "c",
        "version": version,
        "schema": [{"name": table, "properties": props}],
    }


def write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc))
    return path


def codes(violations):
    return [v.code for v in violations]


def test_additive_without_backfill_fails(tmp_path):
    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    head = write(tmp_path, "head.yaml", contract({"a": "string", "b": "string"}, version="1.1.0"))
    violations = policy.check_change(base, head)
    assert codes(violations) == ["expand-without-backfill"]
    assert "t.b" in violations[0].message


def test_additive_with_backfill_passes(tmp_path):
    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    for mode in ("derive", "null-before", "replay"):
        new = {"a": "string", "b": {"type": "string", "props": [("backfill", mode)]}}
        head = write(tmp_path, f"head-{mode}.yaml", contract(new, version="1.1.0"))
        assert policy.check_change(base, head) == []


def test_additive_with_unknown_backfill_fails(tmp_path):
    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    new = {"a": "string", "b": {"type": "string", "props": [("backfill", "yolo")]}}
    head = write(tmp_path, "head.yaml", contract(new, version="1.1.0"))
    violations = policy.check_change(base, head)
    assert codes(violations) == ["invalid-backfill"]
    assert "yolo" in violations[0].message


def test_type_change_without_major_bump_fails(tmp_path):
    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    head = write(tmp_path, "head.yaml", contract({"a": "integer"}, version="1.1.0"))
    violations = policy.check_change(base, head)
    assert codes(violations) == ["breaking-without-major-bump"]
    assert "t.a" in violations[0].message


def test_type_change_with_major_bump_passes(tmp_path):
    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    head = write(tmp_path, "head.yaml", contract({"a": "integer"}, version="2.0.0"))
    assert policy.check_change(base, head) == []


TODAY = date(2026, 11, 14)


def sunset_base(tmp_path, sunset):
    cols = {"a": "string", "b": {"type": "string", "props": [("sunset", sunset)]}}
    return write(tmp_path, "base.yaml", contract(cols))


def test_removal_before_sunset_fails(tmp_path):
    base = sunset_base(tmp_path, date(2026, 12, 1))
    head = write(tmp_path, "head.yaml", contract({"a": "string"}, version="2.0.0"))
    violations = policy.check_change(base, head, today=TODAY)
    assert codes(violations) == ["removed-before-sunset"]
    assert "t.b" in violations[0].message


def test_removal_without_declared_sunset_fails(tmp_path):
    base = write(tmp_path, "base.yaml", contract({"a": "string", "b": "string"}))
    head = write(tmp_path, "head.yaml", contract({"a": "string"}, version="2.0.0"))
    assert codes(policy.check_change(base, head, today=TODAY)) == ["removal-without-sunset"]


def test_removal_after_sunset_without_major_bump_fails(tmp_path):
    base = sunset_base(tmp_path, date(2026, 11, 1))
    head = write(tmp_path, "head.yaml", contract({"a": "string"}, version="1.1.0"))
    assert codes(policy.check_change(base, head, today=TODAY)) == ["breaking-without-major-bump"]


def test_removal_after_sunset_with_major_bump_passes(tmp_path):
    base = sunset_base(tmp_path, date(2026, 11, 1))
    head = write(tmp_path, "head.yaml", contract({"a": "string"}, version="2.0.0"))
    assert policy.check_change(base, head, today=TODAY) == []


def compiled(tmp_path, **files):
    d = tmp_path / "compiled"
    d.mkdir(exist_ok=True)
    for name, sql in files.items():
        (d / f"{name}.sql").write_text(sql)
    return d


def removal_pair(tmp_path):
    base = sunset_base(tmp_path, date(2026, 11, 1))
    head = write(tmp_path, "head.yaml", contract({"a": "string"}, version="2.0.0"))
    return base, head


def test_removal_read_by_hand_sql_is_blocked_naming_the_reader(tmp_path):
    base, head = removal_pair(tmp_path)
    sql = compiled(tmp_path, int_uses_b='select x.b, x.a from "db"."main"."t" as x')
    violations = policy.check_change(base, head, today=TODAY, compiled_sql_dir=sql)
    assert codes(violations) == ["removal-with-readers"]
    assert "int_uses_b" in violations[0].message and "t.b" in violations[0].message


def test_select_star_reader_is_detected(tmp_path):
    base, head = removal_pair(tmp_path)
    sql = compiled(tmp_path, int_star="select * from main.t")
    violations = policy.check_change(base, head, today=TODAY, compiled_sql_dir=sql)
    assert codes(violations) == ["removal-with-readers"]
    assert "int_star" in violations[0].message


def test_same_column_name_on_another_table_is_not_a_reader(tmp_path):
    base, head = removal_pair(tmp_path)
    sql = compiled(tmp_path, int_other="select o.b from main.other as o")
    assert policy.check_change(base, head, today=TODAY, compiled_sql_dir=sql) == []


def test_main_exits_1_and_prints_each_violation(tmp_path, capsys):
    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    head = write(tmp_path, "head.yaml", contract({"a": "string", "b": "string"}, version="1.1.0"))
    assert policy.main([str(base), str(head)]) == 1
    assert "expand-without-backfill" in capsys.readouterr().out


def test_main_exits_0_and_prints_nothing_for_a_clean_change(tmp_path, capsys):
    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    head = write(tmp_path, "head.yaml", contract({"a": "string"}, version="1.0.1"))
    assert policy.main([str(base), str(head)]) == 0
    assert capsys.readouterr().out == ""


def test_script_entry_point_exits_1_on_violation(tmp_path):
    import subprocess
    import sys

    base = write(tmp_path, "base.yaml", contract({"a": "string"}))
    head = write(tmp_path, "head.yaml", contract({"a": "string", "b": "string"}, version="1.1.0"))
    script = Path(__file__).resolve().parent.parent / "contracts" / "policy.py"
    run = subprocess.run(
        [sys.executable, str(script), str(base), str(head)], capture_output=True, text=True
    )
    assert run.returncode == 1
    assert "expand-without-backfill" in run.stdout


def test_main_passes_compiled_dir_to_the_gate(tmp_path, capsys):
    base, head = removal_pair(tmp_path)
    sql = compiled(tmp_path, int_star="select * from main.t")
    assert policy.main([str(base), str(head), str(sql)]) == 1
    assert "removal-with-readers" in capsys.readouterr().out


def test_reader_in_nested_compiled_directory_is_found(tmp_path):
    base, head = removal_pair(tmp_path)
    nested = tmp_path / "compiled" / "models" / "staging"
    nested.mkdir(parents=True)
    (nested / "stg_deep.sql").write_text("select * from main.t")
    root = tmp_path / "compiled"
    violations = policy.check_change(base, head, today=TODAY, compiled_sql_dir=root)
    assert "stg_deep" in violations[0].message
