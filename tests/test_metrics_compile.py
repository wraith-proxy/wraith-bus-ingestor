"""Ossie metrics -> MetricFlow compiler (ticket #55)."""

import copy
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ops" / "compile_metrics.py"
OSSIE = ROOT / "contracts" / "metrics.ossie.yaml"
GENERATED = ROOT / "dbt" / "models" / "marts" / "_generated" / "_semantic.yml"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _compiler():
    return _load(SCRIPT, "compile_metrics")


def _model():
    return yaml.safe_load(OSSIE.read_text())


def _run(out, seed, locale):
    env = dict(os.environ, PYTHONHASHSEED=str(seed), LC_ALL=locale, LANG=locale)
    subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)], check=True, env=env, cwd=ROOT)
    return hashlib.sha256(Path(out).read_bytes()).hexdigest()


def test_compile_is_deterministic_under_two_hash_seeds_and_a_fixed_locale(tmp_path):
    a = _run(tmp_path / "a.yml", 0, "C")
    b = _run(tmp_path / "b.yml", 12345, "C")
    assert a == b


def test_committed_output_matches_a_fresh_compile(tmp_path):
    fresh = _compiler().compile_model(_model())
    assert GENERATED.read_text() == fresh


def test_no_metric_aggregates_a_percentile():
    doc = yaml.safe_load(_compiler().compile_model(_model()))
    assert doc["metrics"], "expected compiled metrics"
    text = yaml.safe_dump(doc).lower()
    for bad in ("percentile", "median", "p90", "p50"):
        assert bad not in text


def test_percentile_metric_is_refused():
    c, m = _compiler(), _model()
    m["metrics"].append(
        {"name": "p90", "expression": {"sql": "SUM(PunctualityProfile.p90DelaySeconds)"}}
    )
    with pytest.raises(c.CompileError, match="percentile"):
        c.compile_model(m)


def test_metric_over_rollup_table_requires_the_real_operator_filter():
    c, m = _compiler(), _model()
    m["metrics"].append(
        {"name": "unfiltered", "expression": {"sql": "SUM(TravelTimeModel.observations)"}}
    )
    with pytest.raises(c.CompileError, match="operator"):
        c.compile_model(m)


def test_rollup_metric_with_filter_compiles_with_the_filter():
    doc = yaml.safe_load(_compiler().compile_model(_model()))
    obs = next(x for x in doc["metrics"] if x["name"] == "eta_observations")
    assert "!= '*'" in obs["filter"]
    assert "operator" in obs["filter"]


def test_composite_primary_key_becomes_one_concatenated_entity():
    doc = yaml.safe_load(_compiler().compile_model(_model()))
    sm = next(s for s in doc["semantic_models"] if s["name"] == "eta_models")
    ent = sm["entities"][0]
    assert ent["type"] == "primary" and "||" in ent["expr"]
    assert "agg_time_dimension" not in sm.get("defaults", {})


def test_metric_bound_to_a_missing_column_fails_validation(tmp_path):
    v = _load(ROOT / "contracts" / "validate.py", "validate")
    contracts = {
        p.name: yaml.safe_load(p.read_text()) for p in (ROOT / "contracts").glob("*.odcs.yaml")
    }
    onto = yaml.safe_load((ROOT / "contracts" / "wraith-bus-ingestor.ontology.yaml").read_text())
    m = copy.deepcopy(_model())
    next(x for x in m["metrics"] if x["name"] == "locality_stop_count")["expression"]["sql"] = (
        "SUM(Locality.noSuchField)"
    )
    path = tmp_path / "m.ossie.yaml"
    path.write_text(yaml.safe_dump(m))
    res = v.Result()
    v.validate_ossie_model(path, onto, contracts, res)
    assert any("locality_stop_count" in f and "noSuchField" in f for f in res.failures)


def test_compiler_refuses_a_metric_over_a_missing_field():
    c, m = _compiler(), _model()
    m["metrics"][0]["expression"]["sql"] = "SUM(Locality.noSuchField)"
    with pytest.raises(c.CompileError, match="noSuchField"):
        c.compile_model(m)
