"""Every ODCS contract must pass `datacontract lint` with no errors or warnings.

Skipped when the datacontract binary is not on PATH (CI image rebuild pending).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CONTRACTS = sorted((Path(__file__).resolve().parent.parent / "contracts").glob("*.odcs.yaml"))


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda p: p.name)
def test_contract_lints_clean(contract):
    binary = shutil.which("datacontract")
    if binary is None:
        pytest.skip("datacontract-cli not installed")
    result = subprocess.run(
        [binary, "lint", str(contract)], capture_output=True, text=True, timeout=120
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "warning" not in output.lower(), output
