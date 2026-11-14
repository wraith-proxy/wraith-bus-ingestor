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


def contract(columns, pk=(), optional=()):
    """columns: names. pk: names that carry primaryKey: true. optional: names not required."""
    props = [
        {
            "name": c,
            "logicalType": "string",
            **({"primaryKey": True} if c in pk else {}),
            **({} if c in optional else {"required": True}),
        }
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


def through_case(**through):
    """A MANY_TO_MANY link Thing->Thing whose join table is contract c, object tbl."""
    onto = ontology(["code"], {"code": "code"})
    spec = {"contract": "c", "object": "tbl", "fromProperty": "code", "toProperty": "code"}
    onto["linkTypes"] = [
        {
            "apiName": "thingLinksThing",
            "cardinality": "MANY_TO_MANY",
            "from": {"objectType": "Thing", "property": "code"},
            "to": {"objectType": "Thing", "property": "code"},
            "through": {**spec, **through},
        }
    ]
    return onto, contract(["code", "n"], pk=["code"])


def test_through_naming_a_missing_column_fails(tmp_path):
    onto, con = through_case(toProperty="nope")
    res = run(tmp_path, onto, con)
    assert any("thingLinksThing" in f and "'nope'" in f for f in res.failures), res.failures


def test_through_naming_a_missing_object_fails(tmp_path):
    onto, con = through_case(object="ghost")
    res = run(tmp_path, onto, con)
    assert any("thingLinksThing" in f and "'ghost'" in f for f in res.failures), res.failures


def test_through_naming_an_unknown_contract_alias_fails(tmp_path):
    onto, con = through_case(contract="zzz")
    res = run(tmp_path, onto, con)
    assert any("thingLinksThing" in f and "'zzz'" in f for f in res.failures), res.failures


def test_through_carried_property_must_exist(tmp_path):
    onto, con = through_case(properties=["n", "missing"])
    res = run(tmp_path, onto, con)
    assert any("thingLinksThing" in f and "'missing'" in f for f in res.failures), res.failures


def test_valid_through_passes(tmp_path):
    onto, con = through_case(properties=["n"])
    assert run(tmp_path, onto, con).failures == []


def type_case(base, logical, cast=False):
    """One bound property: ontology valueType with baseType `base`, contract column `logical`."""
    onto = ontology(["code"], {"code": "code"})
    onto["valueTypes"] = [{"apiName": "Code", "baseType": base}]
    prop = onto["objectTypes"][0]["properties"][0]
    prop["valueType"] = "Code"
    if cast:
        prop["binding"]["cast"] = True
    con = contract(["code"], pk=["code"])
    con["schema"][0]["properties"][0]["logicalType"] = logical
    return onto, con


def test_base_type_the_contract_column_cannot_hold_fails(tmp_path):
    onto, con = type_case("Double", "string")
    res = run(tmp_path, onto, con)
    assert any("Thing.code" in f and "Double" in f and "string" in f for f in res.failures), (
        res.failures
    )


def test_integer_base_type_over_a_number_column_fails(tmp_path):
    """A number column can hold 2.5; an Integer valueType promises it cannot."""
    onto, con = type_case("Integer", "number")
    assert run(tmp_path, onto, con).failures


def test_double_base_type_over_an_integer_column_passes(tmp_path):
    onto, con = type_case("Double", "integer")
    assert run(tmp_path, onto, con).failures == []


def test_declared_cast_excuses_a_type_difference(tmp_path):
    onto, con = type_case("Double", "string", cast=True)
    assert run(tmp_path, onto, con).failures == []


def null_case(required, nullable):
    """One bound, non-key property whose contract column is `required` or not."""
    onto = ontology(["id"], {"id": "id", "code": "code"})
    if nullable:
        onto["objectTypes"][0]["properties"][1]["nullable"] = True
    con = contract(["id", "code"], pk=["id"], optional=[] if required else ["code"])
    return onto, con


def test_optional_contract_column_needs_a_nullable_ontology_property(tmp_path):
    onto, con = null_case(required=False, nullable=False)
    res = run(tmp_path, onto, con)
    assert any("Thing.code" in f and "nullable" in f for f in res.failures), res.failures


def test_nullable_property_over_a_required_column_fails(tmp_path):
    onto, con = null_case(required=True, nullable=True)
    res = run(tmp_path, onto, con)
    assert any("Thing.code" in f and "required" in f for f in res.failures), res.failures


def test_nullability_that_agrees_passes(tmp_path):
    for required, nullable in ((True, False), (False, True)):
        onto, con = null_case(required, nullable)
        assert run(tmp_path, onto, con).failures == [], (required, nullable)


def test_primary_key_column_is_exempt_from_nullability_parity(tmp_path):
    onto = ontology(["id"], {"id": "id"})
    con = contract(["id"], pk=["id"], optional=["id"])
    assert run(tmp_path, onto, con).failures == []


# --- Ossie metrics/objects/links model (contracts/metrics.ossie.yaml) -------------------------


def ossie_run(tmp_path, model, onto, contracts):
    """Validate an in-memory Ossie model against an in-memory Foundry ontology + contracts."""
    path = tmp_path / "m.ossie.yaml"
    path.write_text(yaml.safe_dump(model))
    res = validate.Result()
    validate.validate_ossie_model(path, onto, contracts, res)
    return res


def ossie_case():
    """Foundry ontology `o` (Thing bound to c.tbl) and a matching Ossie model + contracts."""
    onto = ontology(["code"], {"code": "code", "n": "n"})
    contracts = {"c.odcs.yaml": contract(["code", "n"], pk=["code"])}
    model = {
        "version": "0.3.0.dev0",
        "name": "m",
        "datasets": [
            {
                "name": "Thing",
                "source": "c.tbl",
                "primary_key": ["code"],
                "fields": [
                    {"name": "code", "expression": {"sql": "code"}},
                    {"name": "n", "expression": {"sql": "n"}},
                ],
            }
        ],
        "relationships": [],
        "metrics": [{"name": "things", "expression": {"sql": "COUNT(Thing.code)"}}],
    }
    return model, onto, contracts


def test_matching_ossie_model_passes(tmp_path):
    model, onto, contracts = ossie_case()
    assert ossie_run(tmp_path, model, onto, contracts).failures == []


def test_ossie_field_column_missing_from_the_contract_fails(tmp_path):
    model, onto, contracts = ossie_case()
    model["datasets"][0]["fields"][1]["expression"]["sql"] = "ghost"
    res = ossie_run(tmp_path, model, onto, contracts)
    assert any("Thing.n" in f and "'ghost'" in f for f in res.failures), res.failures


def test_ossie_source_object_missing_from_the_contract_fails(tmp_path):
    model, onto, contracts = ossie_case()
    model["datasets"][0]["source"] = "c.nope"
    res = ossie_run(tmp_path, model, onto, contracts)
    assert any("Thing" in f and "'nope'" in f for f in res.failures), res.failures


def test_ossie_field_disagreeing_with_the_foundry_binding_fails(tmp_path):
    """Foundry binds n -> n; the Ossie field n reads a different (real) column."""
    model, onto, contracts = ossie_case()
    model["datasets"][0]["fields"][1]["expression"]["sql"] = "code"
    res = ossie_run(tmp_path, model, onto, contracts)
    assert any("Thing.n" in f and "binding" in f for f in res.failures), res.failures


def test_ossie_dataset_without_a_foundry_object_type_fails(tmp_path):
    model, onto, contracts = ossie_case()
    model["datasets"][0]["name"] = "Other"
    res = ossie_run(tmp_path, model, onto, contracts)
    assert any("Other" in f and "objectType" in f for f in res.failures), res.failures


def test_ossie_relationship_naming_an_undeclared_field_fails(tmp_path):
    model, onto, contracts = ossie_case()
    model["relationships"] = [
        {"name": "r", "from": "Thing", "to": "Thing", "from_columns": ["zzz"], "to_columns": ["code"]}
    ]
    res = ossie_run(tmp_path, model, onto, contracts)
    assert any("r" in f and "'zzz'" in f for f in res.failures), res.failures


def test_ossie_metric_naming_an_undeclared_field_fails(tmp_path):
    model, onto, contracts = ossie_case()
    model["metrics"][0]["expression"]["sql"] = "SUM(Thing.missing)"
    res = ossie_run(tmp_path, model, onto, contracts)
    assert any("things" in f and "Thing.missing" in f for f in res.failures), res.failures


def test_real_ossie_model_binds_cleanly_and_is_pinned():
    root = Path(__file__).resolve().parent.parent / "contracts"
    contracts = {p.name: yaml.safe_load(p.read_text()) for p in root.glob("*.odcs.yaml")}
    onto = yaml.safe_load((root / "wraith-bus-ingestor.ontology.yaml").read_text())
    path = root / "metrics.ossie.yaml"
    res = validate.Result()
    validate.validate_ossie_model(path, onto, contracts, res)
    assert res.failures == []
    head = "\n".join(path.read_text().splitlines()[:30])
    assert "e53fda3c61ebe1bfb169e2c341529426fb348ee7" in head
    assert "c6ecd781c3ec43326432b00a938e5bfe24584877" in head
