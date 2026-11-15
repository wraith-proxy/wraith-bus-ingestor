"""SQL ratchet: model SQL code lines may not grow past the recorded baseline.
Runs on tmp dirs and on the real dbt/models tree; nothing here touches a warehouse."""

from pathlib import Path

import sql_ratchet

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "ops" / "sql_baseline.txt"


def write_model(models, name, text):
    path = models / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_baseline(tmp_path, code, raw):
    path = tmp_path / "baseline.txt"
    path.write_text(f"code={code}\nraw={raw}\n")
    return path


def test_comments_and_blank_lines_are_not_code(tmp_path):
    text = (
        "-- header comment\n\n/* block\n   comment */\nselect 1 as x\n"
        "{# jinja\n comment #}\n\nfrom t -- trailing\n"
    )
    assert sql_ratchet.code_lines(text) == 2
    assert sql_ratchet.raw_lines(text) == 9


def test_measure_counts_every_sql_file_under_models(tmp_path):
    models = tmp_path / "models"
    write_model(models, "a.sql", "select 1\n-- note\n")
    write_model(models, "nested/b.sql", "select 2\n\nselect 3\n")
    write_model(models, "readme.md", "select 9\nselect 9\n")
    assert sql_ratchet.measure(models) == (3, 5)


def test_fails_above_baseline(tmp_path):
    models = tmp_path / "models"
    write_model(models, "a.sql", "select 1\nselect 2\nselect 3\n")
    baseline = write_baseline(tmp_path, code=2, raw=3)
    ok, message = sql_ratchet.check(models, baseline)
    assert ok is False
    assert "3" in message and "2" in message


def test_passes_at_or_below_baseline(tmp_path):
    models = tmp_path / "models"
    write_model(models, "a.sql", "select 1\n-- added comment\n")
    baseline = write_baseline(tmp_path, code=1, raw=2)
    ok, _ = sql_ratchet.check(models, baseline)
    assert ok is True


def test_cli_exit_code_is_nonzero_above_baseline(tmp_path):
    models = tmp_path / "models"
    write_model(models, "a.sql", "select 1\nselect 2\n")
    baseline = write_baseline(tmp_path, code=1, raw=2)
    assert sql_ratchet.main(["--roots", str(models), "--baseline", str(baseline)]) == 1


def test_measure_sums_every_root(tmp_path):
    models, macros, tests = (tmp_path / n for n in ("models", "macros", "tests"))
    write_model(models, "a.sql", "select 1\n")
    write_model(macros, "m.sql", "{% macro m() %}\nselect 2\n{% endmacro %}\n")
    write_model(tests, "t.sql", "select 3\n")
    assert sql_ratchet.measure(models, macros, tests) == (5, 5)


def test_missing_root_counts_as_zero(tmp_path):
    assert sql_ratchet.measure(tmp_path / "nope") == (0, 0)


def test_generated_sql_is_not_hand_written(tmp_path):
    tests = tmp_path / "tests"
    write_model(tests, "t.sql", "select 1\n")
    write_model(tests, "generated/g.sql", "select 2\nselect 3\n")
    assert sql_ratchet.measure(tests) == (1, 1)


def test_macro_growth_alone_fails(tmp_path):
    models, macros = tmp_path / "models", tmp_path / "macros"
    write_model(models, "a.sql", "select 1\n")
    write_model(macros, "m.sql", "select 2\nselect 3\n")
    baseline = write_baseline(tmp_path, code=1, raw=1)
    ok, _ = sql_ratchet.check([models, macros], baseline)
    assert ok is False


def test_fails_below_baseline_until_it_is_lowered(tmp_path):
    models = tmp_path / "models"
    write_model(models, "a.sql", "select 1\n")
    baseline = write_baseline(tmp_path, code=5, raw=5)
    ok, message = sql_ratchet.check(models, baseline)
    assert ok is False
    assert "--update" in message


def test_update_rewrites_baseline_to_measured(tmp_path):
    models = tmp_path / "models"
    write_model(models, "a.sql", "select 1\n-- c\n")
    baseline = write_baseline(tmp_path, code=5, raw=5)
    assert sql_ratchet.main(["--roots", str(models), "--baseline", str(baseline), "--update"]) == 0
    assert sql_ratchet.read_baseline(baseline) == (1, 2)
    assert sql_ratchet.main(["--roots", str(models), "--baseline", str(baseline)]) == 0


def test_baseline_file_matches_measured_tree():
    code, raw = sql_ratchet.read_baseline(BASELINE)
    measured = sql_ratchet.measure(*sql_ratchet.ROOTS)
    assert (code, raw) == measured


def test_real_models_tree_within_baseline():
    ok, message = sql_ratchet.check(sql_ratchet.ROOTS, BASELINE)
    assert ok, message
