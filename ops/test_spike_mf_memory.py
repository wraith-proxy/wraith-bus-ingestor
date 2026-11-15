"""ops/spike_mf_memory.sh guards: refuses a bad WORK dir; --dry-run changes nothing."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "spike_mf_memory.sh"


def _run(tmp_path, work, *args):
    env = dict(os.environ, WORK=str(work))
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
