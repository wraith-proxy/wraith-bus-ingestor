"""SQL ratchet: fail CI when hand-written dbt SQL drifts from the baseline.

Counts dbt/models, dbt/macros and dbt/tests (generated/ subtrees excluded, the
drift test covers those). Gate: non-blank code lines (SQL block comments, Jinja
{# #} comments and -- line comments stripped). Growth fails; a measured count
below the baseline also fails until the baseline is lowered, so shrunk SQL cannot
quietly regrow. Lower it with: python3 ops/sql_ratchet.py --update
Raw line count is recorded in the baseline for context but does not gate.

This module reads .sql files and the baseline file. It must never run SQL or
touch a database, and it only writes the baseline under --update.
"""

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOTS = [REPO / "dbt" / "models", REPO / "dbt" / "macros", REPO / "dbt" / "tests"]
BASELINE = REPO / "ops" / "sql_baseline.txt"

_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_JINJA = re.compile(r"\{#.*?#\}", re.S)
_LINE = re.compile(r"--.*$")


def _keep_newlines(match):
    return "\n" * match.group(0).count("\n")


def strip_comments(text):
    """Remove comments but keep line breaks, so line counting stays aligned.

    Args:
        text: SQL source text.

    Returns:
        The text with block, Jinja and line comments removed.
    """
    text = _BLOCK.sub(_keep_newlines, text)
    text = _JINJA.sub(_keep_newlines, text)
    return "\n".join(_LINE.sub("", line) for line in text.splitlines())


def code_lines(text):
    """Count non-blank lines of code once comments are stripped.

    Args:
        text: SQL source text.

    Returns:
        The number of non-blank code lines.
    """
    return sum(1 for line in strip_comments(text).splitlines() if line.strip())


def raw_lines(text):
    """Count all lines of text.

    Args:
        text: SQL source text.

    Returns:
        The raw line count.
    """
    return len(text.splitlines())


def sql_files(root):
    """List the hand-written .sql files under a root.

    Args:
        root: Directory to search; any `generated` subtree is excluded.

    Returns:
        A sorted list of paths.
    """
    root = Path(root)
    return sorted(p for p in root.rglob("*.sql") if "generated" not in p.relative_to(root).parts)


def measure(*roots):
    """Return (code_lines, raw_lines) summed over every hand-written .sql under roots.

    Args:
        *roots: Directories to measure.

    Returns:
        A tuple of code lines and raw lines.
    """
    code = raw = 0
    for root in roots:
        for path in sql_files(root):
            text = path.read_text()
            code += code_lines(text)
            raw += raw_lines(text)
    return code, raw


def read_baseline(path):
    """Return (code, raw) from a file of key=value lines; '#' starts a comment.

    Args:
        path: Baseline file path.

    Returns:
        A tuple of the code and raw counts.
    """
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = int(value.strip())
    return values["code"], values["raw"]


def _as_roots(roots):
    return [roots] if isinstance(roots, (str, Path)) else list(roots)


def check(roots, baseline):
    """Return (ok, message). ok is False when code lines differ from the baseline.

    Args:
        roots: A directory or an iterable of directories to measure.
        baseline: Baseline file path.

    Returns:
        A tuple of the pass flag and a message.
    """
    code, raw = measure(*_as_roots(roots))
    base_code, base_raw = read_baseline(baseline)
    if code > base_code:
        return False, (
            f"SQL ratchet: {code} code lines exceed baseline {base_code} "
            f"(raw {raw}, baseline raw {base_raw})."
        )
    if code < base_code:
        return False, (
            f"SQL ratchet: {code} code lines are below baseline {base_code}; "
            "lower the baseline with: python3 ops/sql_ratchet.py --update"
        )
    return True, f"SQL ratchet ok: {code} code lines at baseline {base_code} (raw {raw})"


def write_baseline(path, code, raw):
    """Write the baseline file, keeping any existing '#' header lines.

    Args:
        path: Baseline file path.
        code: Code line count.
        raw: Raw line count.
    """
    path = Path(path)
    header = []
    if path.exists():
        header = [ln for ln in path.read_text().splitlines() if ln.startswith("#")]
    path.write_text("\n".join(header + [f"code={code}", f"raw={raw}"]) + "\n")


def main(argv=None):
    """Check the SQL line counts against the baseline, or rewrite it with --update.

    Args:
        argv: Command-line arguments; defaults to `sys.argv[1:]`.

    Returns:
        0 on success, 1 when the counts differ from the baseline.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--roots", type=Path, nargs="+", default=ROOTS)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument(
        "--update", action="store_true", help="rewrite the baseline to the measured counts"
    )
    args = parser.parse_args(argv)
    if args.update:
        code, raw = measure(*args.roots)
        write_baseline(args.baseline, code, raw)
        print(f"SQL ratchet baseline updated: code={code} raw={raw}")
        return 0
    ok, message = check(args.roots, args.baseline)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
