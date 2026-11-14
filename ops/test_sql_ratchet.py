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
    assert sql_ratchet.main(["--models", str(models), "--baseline", str(baseline)]) == 1


def test_baseline_file_holds_both_numbers():
    code, raw = sql_ratchet.read_baseline(BASELINE)
    assert (code, raw) == (653, 1512)


def test_real_models_tree_within_baseline():
    measured = sql_ratchet.measure(REPO / "dbt" / "models")
    ok, message = sql_ratchet.check(REPO / "dbt" / "models", BASELINE)
    assert ok, message
    assert measured[0] <= 653
