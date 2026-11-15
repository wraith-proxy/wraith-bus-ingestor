"""ops/spike_mf_memory.sh guards: refuses a bad WORK dir; --dry-run changes nothing."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "spike_mf_memory.sh"


def _run(tmp_path, work, *args, **extra_env):
    # PGREP=false stubs the "build or extract running" guard: the real pgrep -f matches
    # any host process whose cmdline mentions build.sh / dbt run / extract.py.
    env = dict(os.environ, WORK=str(work), PGREP="false", **extra_env)
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True)


def test_refuses_work_inside_repo(tmp_path):
    r = _run(tmp_path, SCRIPT.parent / "x", "--dry-run")
    assert r.returncode != 0 and "outside the repo" in (r.stdout + r.stderr)


def test_dry_run_creates_nothing(tmp_path):
    work = Path(os.environ["HOME"]) / f"spike-dry-{os.getpid()}"
    r = _run(tmp_path, work, "--dry-run")
    try:
        assert not work.exists(), "dry-run created the work dir"
        assert r.returncode == 0 or "nightly build window" in r.stdout + r.stderr, r.stderr
    finally:
        Path(str(work) + ".log").unlink(missing_ok=True)


def test_leading_zero_hhmm_is_not_octal(tmp_path):
    work = Path(os.environ["HOME"]) / f"spike-oct-{os.getpid()}"
    try:
        for hhmm, inside in (("0809", False), ("0230", False), ("0429", True), ("0431", False), ("0008", False)):
            r = _run(tmp_path, work, "--dry-run", NOW_HHMM=hhmm)
            out = r.stdout + r.stderr
            assert "value too great" not in out and "syntax error" not in out, (hhmm, out)
            assert ("nightly build window" in out) == inside, (hhmm, out)
    finally:
        Path(str(work) + ".log").unlink(missing_ok=True)
