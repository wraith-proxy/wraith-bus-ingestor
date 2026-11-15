"""validate.py in gate mode (--marts given, as build.sh and cutover.sh do) must fail
when nothing was checked; a bare run keeps degrading to a skip (CI has no engines).

No live QuestDB or warehouse: QuestDB points at a closed port, marts at tmp files."""

import importlib.util
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("validate", ROOT / "contracts" / "validate.py")
validate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate)

DEAD_QUESTDB = "http://127.0.0.1:1"
POSITIONS = str(ROOT / "contracts" / "bus-positions.odcs.yaml")


def _run(monkeypatch, tmp_path, *args):
    monkeypatch.setattr(validate, "DEFAULT_MARTS", str(tmp_path / "no-default.duckdb"))
    monkeypatch.setattr(validate, "DEFAULT_GTFS", None)
    monkeypatch.setattr(
        sys, "argv", ["validate.py", "--questdb", DEAD_QUESTDB, "--no-ontology", *args]
    )
    return validate.main()


def test_gate_mode_missing_marts_and_no_questdb_fails(monkeypatch, tmp_path, capsys):
    rc = _run(
        monkeypatch, tmp_path, "--marts", str(tmp_path / "missing.duckdb"), "--contract", POSITIONS
    )
    assert rc == 1
    assert "nothing" in capsys.readouterr().out.lower()


def test_gate_mode_present_marts_but_every_contract_skipped_fails(monkeypatch, tmp_path, capsys):
    marts = tmp_path / "marts.duckdb"
    duckdb.connect(str(marts)).close()
    # bus-positions needs QuestDB, which is down: the one contract is skipped, 0 checks ran
    rc = _run(monkeypatch, tmp_path, "--marts", str(marts), "--contract", POSITIONS)
    assert rc == 1
    assert "nothing" in capsys.readouterr().out.lower()


def test_bare_run_with_no_engines_still_skips_cleanly(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path) == 0


def test_nothing_checked_message_names_the_cause(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, "--marts", str(tmp_path / "missing.duckdb"))
    out = capsys.readouterr().out
    assert "marts unavailable" in out
