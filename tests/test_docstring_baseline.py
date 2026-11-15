"""The docstring lint (#60): Google-style pydocstyle with a baseline that can only shrink.

The seam is ruff. `docstring-baseline.toml` lists the files not yet converted; ruff
reads it (pyproject.toml `extend`) and lets exactly those files fail the D rules. These
tests keep that list honest: every entry is a real file that still fails, a converted
file must leave the list, and the list never grows. None of them reads docstring wording.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docstring-baseline.toml"
BASELINE_REL = "docstring-baseline.toml"

# Ratchet ceiling: the number of unconverted files the baseline may hold. Lower it as
# conversion slices land (never raise it). The seeded size is the count of non-test
# modules that failed the D rules when the rule was switched on.
CEILING = 0

GOOGLE = ["--isolated", "--select", "D", "--config", 'lint.pydocstyle.convention="google"']


def _ruff(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args], cwd=cwd, capture_output=True, text=True
    )


def baseline_entries(text: str) -> list[str]:
    """Files listed in a baseline file's text (its extend-per-file-ignores keys)."""
    return list(tomllib.loads(text)["lint"]["extend-per-file-ignores"])


def stale_entries(entries: list[str], failing: set[str]) -> list[str]:
    """Entries whose file no longer fails the docstring rules, so must leave the list."""
    return [e for e in entries if e not in failing]


def grown_entries(entries: list[str], previous: list[str]) -> list[str]:
    """Entries present now that the previous baseline did not allow."""
    return [e for e in entries if e not in previous]


def _failing(files: list[str]) -> set[str]:
    if not files:
        return set()
    proc = _ruff("check", *GOOGLE, "--output-format", "concise", *files)
    return {line.split(":")[0] for line in proc.stdout.splitlines() if ".py:" in line}


# --- the checks themselves are trustworthy (seeded, so they can be seen red) --


def test_stale_entry_check_flags_a_file_that_now_passes():
    assert stale_entries(["a.py", "b.py"], failing={"a.py"}) == ["b.py"]


def test_growth_check_flags_a_file_added_to_the_baseline():
    assert grown_entries(["a.py", "new.py"], previous=["a.py", "b.py"]) == ["new.py"]
    assert grown_entries(["a.py"], previous=["a.py", "b.py"]) == []


def test_a_missing_docstring_in_a_non_baselined_file_fails_the_lint(tmp_path):
    mod = tmp_path / "newmod.py"
    mod.write_text("def public(x):\n    return x\n")
    proc = _ruff("check", "--select", "D", "--config", str(ROOT / "pyproject.toml"), str(mod))
    assert proc.returncode != 0
    assert "D103" in proc.stdout or "D100" in proc.stdout


def test_a_malformed_google_section_in_a_non_baselined_file_fails_the_lint(tmp_path):
    mod = tmp_path / "newmod.py"
    mod.write_text(
        '"""Module purpose."""\n\n\ndef f(x):\n    """Do it.\n\n'
        "    Args:\n        y: wrong name.\n"
        '    """\n    return x\n'
    )
    proc = _ruff("check", "--select", "D", "--config", str(ROOT / "pyproject.toml"), str(mod))
    assert proc.returncode != 0


def test_tests_are_exempt_from_the_docstring_rules(tmp_path):
    mod = tmp_path / "test_newmod.py"
    mod.write_text("def test_it():\n    assert True\n")
    proc = _ruff("check", "--select", "D", "--config", str(ROOT / "pyproject.toml"), str(mod))
    assert proc.returncode == 0, proc.stdout


# --- the real baseline -------------------------------------------------------


def test_baseline_file_exists_and_ruff_reads_it():
    assert BASELINE.is_file()
    proc = _ruff("check", "--select", "D", ".")
    assert proc.returncode == 0, proc.stdout[-2000:]


def test_baseline_is_sorted_unique_and_lists_only_existing_non_test_modules():
    entries = baseline_entries(BASELINE.read_text())
    assert entries == sorted(set(entries))
    for e in entries:
        assert e.endswith(".py") and (ROOT / e).is_file(), f"baseline lists a missing file: {e}"
        name = Path(e).name
        assert not name.startswith("test_") and "tests/" not in e, f"tests are exempt: {e}"


def test_baseline_lists_only_files_that_still_fail_so_converted_files_leave_it():
    entries = baseline_entries(BASELINE.read_text())
    stale = stale_entries(entries, _failing(entries))
    assert not stale, f"these now pass the docstring rules; remove them from the baseline: {stale}"


def test_baseline_never_grows_past_the_ceiling():
    assert len(baseline_entries(BASELINE.read_text())) <= CEILING


def test_baseline_is_a_subset_of_the_committed_baseline():
    shown = subprocess.run(
        ["git", "show", f"HEAD:{BASELINE_REL}"], cwd=ROOT, capture_output=True, text=True
    )
    if shown.returncode != 0:  # not committed yet (first slice) or no git history
        return
    grown = grown_entries(baseline_entries(BASELINE.read_text()), baseline_entries(shown.stdout))
    assert not grown, f"the baseline may only shrink; these were added: {grown}"
