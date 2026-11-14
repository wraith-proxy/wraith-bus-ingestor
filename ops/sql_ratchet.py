"""SQL ratchet: fail CI when hand-written dbt model SQL grows past the baseline.

Gate: non-blank code lines (SQL block comments, Jinja {# #} comments and
-- line comments stripped). Raw line count is recorded in the baseline for
context but does not gate. Usage: python3 ops/sql_ratchet.py"""

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODELS = REPO / "dbt" / "models"
BASELINE = REPO / "ops" / "sql_baseline.txt"

_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_JINJA = re.compile(r"\{#.*?#\}", re.S)
_LINE = re.compile(r"--.*$")


def _keep_newlines(match):
    return "\n" * match.group(0).count("\n")


def strip_comments(text):
    """Remove comments but keep line breaks, so line counting stays aligned."""
    text = _BLOCK.sub(_keep_newlines, text)
    text = _JINJA.sub(_keep_newlines, text)
    return "\n".join(_LINE.sub("", line) for line in text.splitlines())


def code_lines(text):
    return sum(1 for line in strip_comments(text).splitlines() if line.strip())


def raw_lines(text):
    return len(text.splitlines())


def sql_files(models):
    return sorted(Path(models).rglob("*.sql"))


def measure(models):
    """Return (code_lines, raw_lines) summed over every .sql file under models."""
    code = raw = 0
    for path in sql_files(models):
        text = path.read_text()
        code += code_lines(text)
        raw += raw_lines(text)
    return code, raw


def read_baseline(path):
    """Return (code, raw) from a file of key=value lines; '#' starts a comment."""
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = int(value.strip())
    return values["code"], values["raw"]


def check(models, baseline):
    """Return (ok, message). ok is False only when code lines exceed the baseline."""
    code, raw = measure(models)
    base_code, base_raw = read_baseline(baseline)
    if code > base_code:
        return False, (
            f"SQL ratchet: {code} code lines exceed baseline {base_code} "
            f"(raw {raw}, baseline raw {base_raw})."
        )
    return True, f"SQL ratchet ok: {code} code lines within baseline {base_code} (raw {raw})"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", type=Path, default=MODELS)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args(argv)
    ok, message = check(args.models, args.baseline)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
