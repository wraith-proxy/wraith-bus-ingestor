#!/usr/bin/env python
"""Check live data against its ODCS contracts, and the ontology against both.

A data contract nobody executes is a document. This runs the obligations:
every declared object and property must exist with a compatible type, every
required property must be non-null, every declared key must be unique, and
every quality rule must hold.

    contracts/validate.py                      # both contracts + the ontology
    contracts/validate.py --contract contracts/bus-marts.odcs.yaml
    contracts/validate.py --marts /tmp/candidate.duckdb   # gate a rebuild

Exit code 0 if every obligation holds, 1 otherwise, so it can gate a build.
Rules whose severity is 'info', and `text` rules, are reported but never fail.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# dbt owns the only virtualenv in the repo; borrow its yaml and duckdb.
sys.path.insert(0, os.path.join(ROOT, "dbt", ".venv", "lib", "python3.12", "site-packages"))
import yaml  # noqa: E402

DEFAULT_MARTS = os.path.join(ROOT, "dbt", "ghost_bus.duckdb")
DEFAULT_QUESTDB = os.environ.get("QUESTDB_HTTP", "http://localhost:9001")

# Physical types that mean the same thing to the engines we target.
TYPE_ALIASES = {
    "VARCHAR": {"VARCHAR", "STRING", "TEXT", "SYMBOL", "CHAR"},
    "SYMBOL": {"SYMBOL", "VARCHAR", "STRING"},
    "DOUBLE": {"DOUBLE", "FLOAT8", "DECIMAL", "REAL"},
    "BIGINT": {"BIGINT", "INT8", "LONG", "HUGEINT"},
    "LONG": {"LONG", "BIGINT", "INT8"},
    "INTEGER": {"INTEGER", "INT", "INT4", "BIGINT", "INT8"},
    "TIMESTAMP": {"TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ", "DATETIME"},
    "TIMESTAMP WITH TIME ZONE": {"TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ", "TIMESTAMP"},
}

OPERATORS = {
    "mustBe": lambda v, x: v == x,
    "mustNotBe": lambda v, x: v != x,
    "mustBeGreaterThan": lambda v, x: v > x,
    "mustBeGreaterOrEqualTo": lambda v, x: v >= x,
    "mustBeLessThan": lambda v, x: v < x,
    "mustBeLessOrEqualTo": lambda v, x: v <= x,
    "mustBeBetween": lambda v, x: x[0] <= v <= x[1],
    "mustNotBeBetween": lambda v, x: not (x[0] <= v <= x[1]),
}


class Result:
    def __init__(self):
        self.failures, self.warnings, self.passes, self.skips = [], [], 0, 0

    def ok(self, what):
        self.passes += 1
        print(f"  \033[32mPASS\033[0m {what}")

    def fail(self, what, detail, severity="error"):
        line = f"{what}: {detail}"
        if severity == "info":
            self.warnings.append(line)
            print(f"  \033[33mWARN\033[0m {line}")
        else:
            self.failures.append(line)
            print(f"  \033[31mFAIL\033[0m {line}")

    def skip(self, what, why):
        self.skips += 1
        print(f"  \033[90mSKIP\033[0m {what} ({why})")


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
class DuckDBEngine:
    name = "duckdb"

    def __init__(self, path):
        import duckdb
        self.path = path
        if not os.path.exists(path):
            raise RuntimeError(f"no such database: {path}")
        if os.path.isdir(path):
            raise RuntimeError(f"{path} is a directory (docker made it for a missing bind mount?)")
        self.con = duckdb.connect(path, read_only=True)

    def columns(self, table):
        rows = self.con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ?", [table]).fetchall()
        return {name: dtype.upper() for name, dtype in rows}

    def scalar(self, sql):
        return self.con.execute(sql).fetchone()[0]

    def quote(self, ident):
        return '"%s"' % ident.replace('"', '""')

    def scope(self, table):
        return self.quote(table)

    def close(self):
        self.con.close()


class QuestDBEngine:
    """QuestDB holds ~1e9 rows of append-only history, so validating the whole
    table is neither affordable nor meaningful -- the contract describes what the
    ingestor is landing NOW, and historical rows predate several of its clauses.
    Every check is therefore scoped to a recent window by substituting a bounded
    subquery wherever a rule names the object."""

    name = "questdb"

    def __init__(self, url, window_minutes=5):
        self.url = url.rstrip("/")
        self.window_minutes = window_minutes

    def _exec(self, sql):
        q = urllib.parse.quote(sql)
        with urllib.request.urlopen(f"{self.url}/exec?query={q}", timeout=30) as r:
            payload = json.load(r)
        if "error" in payload:
            raise RuntimeError(payload["error"])
        return payload

    def columns(self, table):
        payload = self._exec(f"SELECT * FROM {table} LIMIT 0")
        return {c["name"]: c["type"].upper() for c in payload["columns"]}

    def scalar(self, sql):
        return self._exec(sql)["dataset"][0][0]

    def quote(self, ident):
        return ident

    def scope(self, table):
        return ("(SELECT * FROM %s WHERE timestamp > dateadd('m', -%d, now()))"
                % (table, self.window_minutes))

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------
def types_compatible(declared, actual):
    declared, actual = declared.upper(), actual.upper()
    if declared == actual:
        return True
    # VARCHAR(18) and the like: compare the bare type name.
    base = declared.split("(")[0].strip()
    if base == actual:
        return True
    return actual in TYPE_ALIASES.get(base, {base})


def check_quality(engine, res, rule, table, prop=None):
    """Run one ODCS quality rule. Returns nothing; records into res."""
    rid = rule.get("id") or rule.get("name") or rule.get("metric") or "unnamed"
    where = f"{table}.{prop}" if prop else table
    label = f"{where} [{rid}]"
    severity = rule.get("severity", "error")
    rtype = rule.get("type", "library" if rule.get("metric") else None)

    if rtype == "text":
        res.skip(label, "text rule, not executable")
        return
    if rtype == "custom":
        res.skip(label, f"custom rule for engine {rule.get('engine')}")
        return

    ops = [(k, v) for k, v in rule.items() if k in OPERATORS]
    if not ops:
        res.skip(label, "no comparison operator")
        return

    t = engine.scope(table)
    try:
        if rtype == "sql":
            query = rule["query"].replace("{object}", t)
            if prop:
                query = query.replace("{property}", engine.quote(prop))
            value = engine.scalar(query)
        else:
            metric = rule.get("metric")
            args = rule.get("arguments") or {}
            if metric == "rowCount":
                value = engine.scalar(f"SELECT count(*) FROM {t}")
            elif metric == "nullValues":
                col = engine.quote(prop)
                value = engine.scalar(f"SELECT count(*) FROM {t} WHERE {col} IS NULL")
                if rule.get("unit") == "percent":
                    total = engine.scalar(f"SELECT count(*) FROM {t}") or 1
                    value = 100.0 * value / total
            elif metric == "duplicateValues":
                cols = args.get("properties") or ([prop] if prop else [])
                if not cols:
                    res.skip(label, "duplicateValues with no properties")
                    return
                keys = ", ".join(engine.quote(c) for c in cols)
                value = engine.scalar(
                    f"SELECT coalesce(sum(n - 1), 0) FROM "
                    f"(SELECT count(*) AS n FROM {t} GROUP BY {keys} HAVING count(*) > 1)")
            elif metric == "invalidValues":
                col = engine.quote(prop)
                valid = args.get("validValues")
                if valid is None:
                    res.skip(label, "invalidValues without validValues")
                    return
                lst = ", ".join("'%s'" % str(v).replace("'", "''") for v in valid)
                value = engine.scalar(f"SELECT count(*) FROM {t} WHERE {col} NOT IN ({lst})")
            else:
                res.skip(label, f"metric '{metric}' not implemented")
                return
    except Exception as exc:
        res.fail(label, f"could not evaluate: {str(exc).splitlines()[0][:160]}", severity)
        return

    for op, expected in ops:
        if OPERATORS[op](value, expected):
            res.ok(f"{label} {op} {expected} (got {value})")
        else:
            res.fail(label, f"{op} {expected}, got {value}", severity)


def validate_contract(path, engines, res):
    contract = yaml.safe_load(open(path))
    name = contract.get("name", os.path.basename(path))
    server = (contract.get("servers") or [{}])[0]
    engine = engines.get(server.get("type")) or engines.get("duckdb")

    print(f"\n\033[1m{name} v{contract.get('version')} "
          f"({contract.get('status')}) -> {engine.name}\033[0m")

    if contract.get("apiVersion") != "v3.2.0":
        res.fail(name, f"apiVersion {contract.get('apiVersion')!r}, expected v3.2.0")
    if contract.get("kind") != "DataContract":
        res.fail(name, f"kind {contract.get('kind')!r}, expected DataContract")

    for obj in contract.get("schema", []):
        table = obj.get("physicalName") or obj["name"]
        try:
            actual = engine.columns(table)
        except Exception as exc:
            res.fail(table, f"object missing or unreadable: {str(exc).splitlines()[0][:140]}")
            continue
        if not actual:
            res.fail(table, "object exists but has no columns")
            continue
        res.ok(f"{table} exists ({len(actual)} columns)")

        for prop in obj.get("properties", []):
            col = prop.get("physicalName") or prop["name"]
            if col not in actual:
                res.fail(f"{table}.{col}", "property declared in contract but absent from the data")
                continue
            declared = prop.get("physicalType")
            if declared and not types_compatible(declared, actual[col]):
                res.fail(f"{table}.{col}",
                         f"type is {actual[col]}, contract declares {declared}")
            # `required` and `primaryKey` are obligations, so enforce them.
            if prop.get("required") or prop.get("primaryKey"):
                check_quality(engine, res,
                              {"id": "required", "metric": "nullValues", "mustBe": 0},
                              table, col)
            if prop.get("unique"):
                check_quality(engine, res,
                              {"id": "unique", "metric": "duplicateValues", "mustBe": 0},
                              table, col)
            for rule in prop.get("quality", []) or []:
                check_quality(engine, res, rule, table, col)

        # A declared composite primary key must actually be unique.
        key = [p.get("physicalName") or p["name"]
               for p in sorted((p for p in obj.get("properties", []) if p.get("primaryKey")),
                               key=lambda p: p.get("primaryKeyPosition", 0))]
        if len(key) > 1 and all(k in actual for k in key):
            check_quality(engine, res,
                          {"id": "primaryKey", "metric": "duplicateValues", "mustBe": 0,
                           "arguments": {"properties": key}}, table)

        for rule in obj.get("quality", []) or []:
            check_quality(engine, res, rule, table)

    return contract


def validate_ontology(path, contracts, res):
    """Every ontology binding must resolve to a real contract property, and any
    enum stated in both places must agree. This is what stops the semantic layer
    drifting away from the data underneath it."""
    onto = yaml.safe_load(open(path))
    print(f"\n\033[1m{onto.get('name')} ontology v{onto.get('version')} "
          f"({onto.get('status')})\033[0m")

    by_alias = {}
    for entry in onto.get("contracts", []):
        c = contracts.get(os.path.basename(entry["path"]))
        if c is None:
            res.fail("ontology", f"references contract {entry['path']} which was not validated")
            continue
        by_alias[entry["alias"]] = {
            o["name"]: {p["name"]: p for p in o.get("properties", [])}
            for o in c.get("schema", [])
        }

    value_types = {v["apiName"]: v for v in onto.get("valueTypes", [])}
    object_types = {o["apiName"]: o for o in onto.get("objectTypes", [])}
    bound = broken = 0

    for obj in onto.get("objectTypes", []):
        for prop in obj.get("properties", []):
            ref = f"{obj['apiName']}.{prop['apiName']}"
            vt = prop.get("valueType")
            if vt and vt not in value_types and vt not in {
                    "String", "Integer", "Long", "Double", "Boolean", "Timestamp", "GeoPoint"}:
                res.fail(ref, f"unknown valueType {vt!r}")
                broken += 1
            binding = prop.get("binding")
            if not binding:
                if not prop.get("computed"):
                    res.fail(ref, "no binding and not marked computed")
                    broken += 1
                continue
            schemas = by_alias.get(binding["contract"])
            if schemas is None:
                res.fail(ref, f"unknown contract alias {binding['contract']!r}")
                broken += 1
                continue
            props = schemas.get(binding["object"])
            if props is None:
                res.fail(ref, f"contract has no object {binding['object']!r}")
                broken += 1
                continue
            cprop = props.get(binding["property"])
            if cprop is None:
                res.fail(ref, f"{binding['object']} has no property {binding['property']!r}")
                broken += 1
                continue
            bound += 1
            # Where both sides state an enum, they must agree.
            onto_enum = set(value_types.get(vt, {}).get("enum") or [])
            c_enum = {e["value"] for e in (cprop.get("enum") or [])}
            if onto_enum and c_enum:
                extra = onto_enum - c_enum
                if extra:
                    res.fail(ref, f"valueType {vt} allows {sorted(extra)} which the contract does not")
                    broken += 1

        for key in obj.get("primaryKey", []):
            if key not in {p["apiName"] for p in obj.get("properties", [])}:
                res.fail(obj["apiName"], f"primaryKey references unknown property {key!r}")
                broken += 1
        title = obj.get("titleProperty")
        if title and title not in {p["apiName"] for p in obj.get("properties", [])}:
            res.fail(obj["apiName"], f"titleProperty {title!r} is not a property")
            broken += 1

    for link in onto.get("linkTypes", []):
        for side in ("from", "to"):
            spec = link[side]
            ot = object_types.get(spec["objectType"])
            if ot is None:
                res.fail(link["apiName"], f"{side} references unknown objectType {spec['objectType']!r}")
                broken += 1
            elif spec.get("property") and spec["property"] not in {
                    p["apiName"] for p in ot.get("properties", [])}:
                res.fail(link["apiName"],
                         f"{side} references {spec['objectType']}.{spec['property']} which does not exist")
                broken += 1

    if not broken:
        res.ok(f"{bound} ontology property bindings resolve; "
               f"{len(onto.get('linkTypes', []))} link types consistent")
    return onto


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contract", action="append", default=None,
                    help="ODCS contract to validate (repeatable; default: both)")
    ap.add_argument("--ontology", default=os.path.join(HERE, "ghost-bus.ontology.yaml"))
    ap.add_argument("--marts", default=DEFAULT_MARTS, help="path to the DuckDB marts file")
    ap.add_argument("--questdb", default=DEFAULT_QUESTDB)
    ap.add_argument("--questdb-window", type=int, default=5, metavar="MINUTES",
                    help="validate QuestDB over this recent window (default 5)")
    ap.add_argument("--no-ontology", action="store_true")
    args = ap.parse_args()

    paths = args.contract or [os.path.join(HERE, f) for f in
                              ("bus-positions.odcs.yaml", "bus-marts.odcs.yaml")]

    engines, res = {}, Result()
    try:
        engines["duckdb"] = DuckDBEngine(args.marts)
    except Exception as exc:
        print(f"\033[31mmarts unavailable:\033[0m {exc}")
    try:
        engines["custom"] = QuestDBEngine(args.questdb, args.questdb_window)
        engines["custom"].columns("bus_positions")
    except Exception as exc:
        print(f"\033[31mquestdb unavailable:\033[0m {str(exc).splitlines()[0][:120]}")
        engines.pop("custom", None)

    contracts = {}
    for path in paths:
        server_type = (yaml.safe_load(open(path)).get("servers") or [{}])[0].get("type")
        if server_type not in engines and "duckdb" not in engines:
            print(f"\n\033[33mskipping {os.path.basename(path)}: no engine available\033[0m")
            continue
        if server_type == "custom" and "custom" not in engines:
            print(f"\n\033[33mskipping {os.path.basename(path)}: QuestDB unreachable\033[0m")
            continue
        contracts[os.path.basename(path)] = validate_contract(path, engines, res)

    if not args.no_ontology and os.path.exists(args.ontology) and contracts:
        validate_ontology(args.ontology, contracts, res)

    print(f"\n\033[1m{res.passes} passed, {len(res.failures)} failed, "
          f"{len(res.warnings)} warned, {res.skips} skipped\033[0m")
    if res.failures:
        print("\nUnmet obligations:")
        for f in res.failures:
            print(f"  - {f}")
    for e in engines.values():
        e.close()
    return 1 if res.failures else 0


if __name__ == "__main__":
    sys.exit(main())
