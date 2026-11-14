"""validate_ontology: ontology objects must stay consistent with the contracts they bind to.
No database is touched; contracts are passed in as dicts."""

import importlib.util
from pathlib import Path

import yaml

_SPEC = importlib.util.spec_from_file_location(
    "validate", Path(__file__).resolve().parent.parent / "contracts" / "validate.py"
)
validate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate)


def run(tmp_path, ontology, contract):
    """Validate an in-memory ontology against one in-memory contract; return Result."""
    path = tmp_path / "o.ontology.yaml"
    path.write_text(yaml.safe_dump(ontology))
    res = validate.Result()
    validate.validate_ontology(path, {"c.odcs.yaml": contract}, res)
    return res


def contract(columns, pk=()):
    """columns: names. pk: names that carry primaryKey: true."""
    props = [
        {"name": c, "logicalType": "string", **({"primaryKey": True} if c in pk else {})}
        for c in columns
    ]
    return {"schema": [{"name": "tbl", "properties": props}]}


def ontology(pk, props, grain="row"):
    """props: {apiName: contract column}. grain: backingDatasets grain, or None to omit."""
    backing = {"contract": "c", "object": "tbl", **({"grain": grain} if grain else {})}
    return {
        "name": "o",
        "version": "1",
        "status": "draft",
        "contracts": [{"alias": "c", "path": "contracts/c.odcs.yaml"}],
        "objectTypes": [
            {
                "apiName": "Thing",
                "primaryKey": pk,
                "backingDatasets": [backing],
                "properties": [
                    {
                        "apiName": api,
                        "binding": {"contract": "c", "object": "tbl", "property": col},
                    }
                    for api, col in props.items()
                ],
            }
        ],
    }


def test_primary_key_must_match_the_contracts_primary_key(tmp_path):
    onto = ontology(["operatorRef", "busId"], {"operatorRef": "operator", "busId": "bus_id"})
    res = run(tmp_path, onto, contract(["operator", "bus_id"], pk=["bus_id"]))
    assert any("primaryKey" in f and "Thing" in f for f in res.failures), res.failures


def test_derived_object_is_not_held_to_the_contracts_primary_key(tmp_path):
    """No grain marker means a derived view (distinct values, aggregate): other key is fine."""
    onto = ontology(["operatorRef"], {"operatorRef": "operator"}, grain=None)
    res = run(tmp_path, onto, contract(["operator", "bus_id"], pk=["bus_id"]))
    assert res.failures == []


def test_real_ontology_binds_cleanly_to_real_contracts(tmp_path):
    root = Path(__file__).resolve().parent.parent / "contracts"
    contracts = {p.name: yaml.safe_load(p.read_text()) for p in root.glob("*.odcs.yaml")}
    res = validate.Result()
    validate.validate_ontology(root / "wraith-bus-ingestor.ontology.yaml", contracts, res)
    assert res.failures == []


def enum_case(contract_values, onto_values):
    """One bound property whose valueType and contract column both state an enum."""
    onto = ontology(["code"], {"code": "code"})
    onto["valueTypes"] = [{"apiName": "Code", "baseType": "String", "enum": onto_values}]
    onto["objectTypes"][0]["properties"][0]["valueType"] = "Code"
    con = contract(["code"], pk=["code"])
    con["schema"][0]["properties"][0]["enum"] = [{"value": v} for v in contract_values]
    return onto, con


def test_contract_enum_value_the_ontology_forbids_fails(tmp_path):
    onto, con = enum_case(["a", "b", "*"], ["a", "b"])
    res = run(tmp_path, onto, con)
    assert any("'*'" in f and "Thing.code" in f for f in res.failures), res.failures


def test_matching_enums_pass(tmp_path):
    onto, con = enum_case(["a", "b"], ["a", "b"])
    assert run(tmp_path, onto, con).failures == []


def pattern_case(pattern, examples):
    """One bound property whose valueType states a pattern and whose contract lists examples."""
    onto = ontology(["code"], {"code": "code"})
    onto["valueTypes"] = [
        {"apiName": "Code", "baseType": "String", "constraints": {"pattern": pattern}}
    ]
    onto["objectTypes"][0]["properties"][0]["valueType"] = "Code"
    con = contract(["code"], pk=["code"])
    con["schema"][0]["properties"][0]["examples"] = examples
    return onto, con


def test_contract_example_the_value_type_pattern_rejects_fails(tmp_path):
    onto, con = pattern_case("^[0-9]{4}[A-Za-z0-9]+$", ["1800STIN0P1", "k28"])
    res = run(tmp_path, onto, con)
    assert any("'k28'" in f and "Thing.code" in f for f in res.failures), res.failures


def test_examples_matching_the_pattern_pass(tmp_path):
    onto, con = pattern_case("^[0-9]{4}[A-Za-z0-9]+$", ["1800STIN0P1"])
    assert run(tmp_path, onto, con).failures == []
