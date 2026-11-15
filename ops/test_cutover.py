"""ops/cutover.sh end to end on throwaway warehouses with stubbed builds.
Never touches the real warehouse, dbt or QuestDB."""
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

SCRIPT = Path(__file__).resolve().parent / "cutover.sh"
STAGES = ["STAGE 0 preflight", "STAGE 1 backup", "STAGE 2 copies", "STAGE 3 build",
          "STAGE 4 compare", "STAGE 5 validate", "STAGE 6 swap"]

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="script refuses root")

# stub build: the baseline gets the old numbers, the candidate the new ones
STUB = (sys.executable + " - <<'PY'\nimport duckdb, os\n"
        "role = os.environ['CUTOVER_ROLE']\n"
        "con = duckdb.connect('wraith_bus_ingestor_build.duckdb')\n"
        "con.execute('drop table if exists localities')\n"
        "con.execute(os.environ['STUB_' + role.upper()])\nPY\n")
SAME = "create table localities as select 'a' as code, 1.5::double as d"


def setup(tmp_path, cand):
    live = tmp_path / "dbt"
    live.mkdir()
    db = live / "wraith_bus_ingestor_build.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table history as select 1 as n")
    con.close()
    (live / "data").mkdir()
    env = dict(os.environ, CUTOVER_DB=str(db), CUTOVER_DATA=str(live / "data"),
               CUTOVER_WORK=str(live / "cutover-work"),
               CUTOVER_BACKUP_DIR=str(tmp_path / "backup"),
               CUTOVER_LOGDIR=str(tmp_path / "logs"), CUTOVER_UNIT="none",
               CUTOVER_MIN_MEM_MB="1", CUTOVER_TABLES="localities",
               CUTOVER_BUILD_CMD=STUB, CUTOVER_SRC_ONLY="1",
               CUTOVER_LINT_CMD="true", CUTOVER_VALIDATE_CMD="true",
               CUTOVER_TEST_CMD="true",
               STUB_BASELINE=SAME, STUB_CANDIDATE=cand)
    return db, env


def run(env, *args):
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True)


def test_dry_run_echoes_all_stages_and_writes_nothing(tmp_path):
    db, env = setup(tmp_path, SAME)
    before = db.stat().st_mtime_ns
    r = run(env, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    for s in STAGES:
        assert s in r.stdout, s
    assert db.stat().st_mtime_ns == before
    assert not (tmp_path / "backup").exists()
    assert not Path(env["CUTOVER_WORK"]).exists()


def test_key_difference_fails_and_leaves_live_file(tmp_path):
    db, env = setup(tmp_path, "create table localities as select 'z' as code, 1.5::double as d")
    r = run(env)
    assert r.returncode == 1 and "compare: FAIL" in r.stdout, r.stdout + r.stderr
    assert "STAGE 6 swap" not in r.stdout
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) from history").fetchone()[0] == 1


def test_1e9_double_difference_swaps_and_keeps_old(tmp_path):
    db, env = setup(
        tmp_path, "create table localities as select 'a' as code, 1.5::double + 1e-9 as d")
    r = run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) from localities").fetchone()[0] == 1
    assert con.execute("select count(*) from history").fetchone()[0] == 1  # history rode along
    kept = list(db.parent.glob("wraith_bus_ingestor_build.duckdb.pre-cutover-*"))
    assert len(kept) == 1
    old = duckdb.connect(str(kept[0]), read_only=True)
    assert old.execute("select count(*) from history").fetchone()[0] == 1
    assert list((tmp_path / "logs").glob("cutover-*/cutover.log"))


def test_stops_without_owner_go_for_live_validate(tmp_path):
    db, env = setup(tmp_path, SAME)
    env["CUTOVER_VALIDATE_CMD"] = ""   # real validate wanted, no go given
    r = run(env, "--no-prompt")
    assert r.returncode == 3 and "owner go" in r.stdout
    assert not list(db.parent.glob("*.pre-cutover-*"))


def test_preflight_refuses_low_memory(tmp_path):
    _, env = setup(tmp_path, SAME)
    env["CUTOVER_MIN_MEM_MB"] = "99999999"
    r = run(env, "--dry-run")
    assert r.returncode != 0 and "memory" in r.stdout + r.stderr


def test_default_build_cmd_is_absolute_and_needs_packages(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    _, env = setup(tmp_path, SAME)
    del env["CUTOVER_BUILD_CMD"]
    r = run(env, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{repo}/.venv/bin/dbt run" in r.stdout
    assert "../.venv" not in r.stdout
    # real (non-dry) run without the gitignored packages must fail loudly, early
    env.pop("CUTOVER_SRC_ONLY")
    env["CUTOVER_PACKAGES"] = str(tmp_path / "no-such-packages")
    r = run(env, "--no-prompt")
    assert r.returncode != 0
    assert "dbt_packages" in r.stdout and "dbt deps" in r.stdout
    assert not (tmp_path / "backup").exists()


def test_tools_do_not_depend_on_uv(tmp_path):
    """CI's image has no .venv; `uv run` there would resolve and download the project."""
    _, env = setup(tmp_path, SAME)
    r = run(env, "--dry-run")
    line = next(ln for ln in (r.stdout + r.stderr).splitlines() if "tools: python=" in ln)
    assert "uv" not in line.split("python=")[1].split()[0].split("/")
    assert "uv run" not in (Path(__file__).parent / "cutover.sh").read_text()


def _stub(tmp_path, marker=None):
    s = tmp_path / "state.sh"
    cond = f'[ -e "{marker}" ] && ' if marker else ""
    s.write_text(f'#!/bin/sh\n{cond}[ "$1" = "$STUB_ACTIVE" ] && echo active || echo inactive\n')
    s.chmod(0o755)
    return str(s)


@pytest.mark.parametrize("unit", ["wraith-bus-build.service", "wraith-bus-extract.service",
                                  "wraith-bus-extract.path"])
def test_active_unit_blocks_preflight(tmp_path, unit):
    _, env = setup(tmp_path, SAME)
    env.update(CUTOVER_STATE_CMD=_stub(tmp_path), STUB_ACTIVE=unit,
               CUTOVER_UNIT="wraith-bus-build.service")
    r = run(env, "--dry-run")
    assert r.returncode != 0 and unit in r.stdout


def test_build_started_during_prompt_blocks_swap(tmp_path):
    db, env = setup(tmp_path, SAME)
    marker = tmp_path / "started"
    env.update(CUTOVER_UNIT="wraith-bus-build.service",
               CUTOVER_STATE_CMD=_stub(tmp_path, marker),
               STUB_ACTIVE="wraith-bus-build.service",
               CUTOVER_VALIDATE_CMD=f"touch {marker}")
    r = run(env, "--no-prompt")
    assert r.returncode == 4 and "not swapping" in r.stdout, r.stdout
    assert not list(db.parent.glob("*.pre-cutover-*"))


def test_work_guard_refuses_data_and_foreign_dirs(tmp_path):
    db, env = setup(tmp_path, SAME)
    data = Path(env["CUTOVER_DATA"])
    for bad in (data, data / "sub"):
        env["CUTOVER_WORK"] = str(bad)
        r = run(env, "--dry-run")
        assert r.returncode != 0 and "WORK" in r.stdout
    foreign = db.parent / "precious"
    foreign.mkdir()
    (foreign / "x").write_text("keep")
    env["CUTOVER_WORK"] = str(foreign)
    r = run(env, "--no-prompt")
    assert r.returncode != 0 and (foreign / "x").exists()
    repo = Path(__file__).resolve().parent.parent
    env["CUTOVER_WORK"] = str(repo / "dbt" / "data")
    assert run(env, "--dry-run").returncode != 0


def test_same_tree_baseline_refused(tmp_path):
    _, env = setup(tmp_path, SAME)
    env.pop("CUTOVER_SRC_ONLY")
    env["CUTOVER_OLD_REF"] = "HEAD"
    r = run(env, "--dry-run")
    assert r.returncode != 0 and "same tree" in r.stdout and "CUTOVER_OLD_REF" in r.stdout


def test_default_compare_covers_history_tables_and_dbt_test_is_planned(tmp_path):
    _, env = setup(tmp_path, SAME)
    del env["CUTOVER_TABLES"]
    del env["CUTOVER_TEST_CMD"]
    r = run(env, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    compare = next(ln for ln in r.stdout.splitlines() if "would: compare" in ln)
    for t in ("eta_models", "localities", "line_punctuality", "int_eta_daily_stats",
              "int_journey_delays"):
        assert t in compare, compare
    assert "dbt test" in r.stdout and "candidate" in r.stdout


def test_dbt_test_runs_in_candidate_copy_before_swap(tmp_path):
    db, env = setup(tmp_path, SAME)
    where = tmp_path / "where"
    env["CUTOVER_TEST_CMD"] = f"pwd > {where}"
    r = run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert where.read_text().strip().endswith("cutover-work/candidate/dbt")
    assert r.stdout.index("dbt test") < r.stdout.index("STAGE 6 swap")


def test_failing_dbt_test_blocks_swap(tmp_path):
    db, env = setup(tmp_path, SAME)
    env["CUTOVER_TEST_CMD"] = "echo FAIL assert_x; false"
    r = run(env)
    assert r.returncode == 1 and "dbt test failed" in r.stdout, r.stdout + r.stderr
    assert "STAGE 5 validate" not in r.stdout and "STAGE 6 swap" not in r.stdout
    assert not list(db.parent.glob("*.pre-cutover-*"))
    assert list((tmp_path / "logs").glob("cutover-*/test-candidate.log"))
