"""Compare two DuckDB warehouses table by table (ops/cutover.sh stage 4).

Exact: row counts, and every non-DOUBLE column (the keys and everything else;
EXCEPT both ways). Tolerant: DOUBLE columns, because DuckDB's 3-thread sums
are not bit-reproducible (profiles.yml threads: 3). Rows are paired on the
non-DOUBLE columns, so a table whose non-DOUBLE columns repeat is compared
as a multiset of those columns and may need a `keys` override.

    cutover_compare.py BASELINE.duckdb CANDIDATE.duckdb TABLE [TABLE ...]

Exit 0 identical within tolerance, 1 otherwise. Opens both files read-only.

This module must never write to either warehouse.
"""
import sys
from dataclasses import dataclass, field

import duckdb

ATOL = 1e-6
RTOL = 1e-9


@dataclass
class Result:
    """Outcome of a comparison.

    Attributes:
        problems: One message per mismatch found; empty means identical.
        ok: True when there are no problems.
    """

    problems: list = field(default_factory=list)

    @property
    def ok(self):
        """Whether the comparison found no problems."""
        return not self.problems


def _columns(con, schema, table):
    return con.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_catalog = ? and table_name = ? order by ordinal_position",
        [schema, table]).fetchall()


def compare(baseline, candidate, tables, atol=ATOL, rtol=RTOL, keys=None):
    """Compare two DuckDB warehouses table by table.

    Args:
        baseline: Path to the baseline warehouse file.
        candidate: Path to the candidate warehouse file.
        tables: Table names to compare.
        atol: Absolute tolerance for DOUBLE columns.
        rtol: Relative tolerance for DOUBLE columns.
        keys: Optional mapping of table name to the columns that pair rows,
            overriding the default of every non-DOUBLE column.

    Returns:
        A `Result` listing every mismatch found.
    """
    res = Result()
    con = duckdb.connect()
    con.execute(f"attach '{baseline}' as a (read_only)")
    con.execute(f"attach '{candidate}' as b (read_only)")
    for t in tables:
        ca, cb = _columns(con, "a", t), _columns(con, "b", t)
        if not ca or not cb:
            res.problems.append(f"{t}: table missing (baseline {bool(ca)}, candidate {bool(cb)})")
            continue
        if ca != cb:
            res.problems.append(f"{t}: key/column schema differs: {ca} vs {cb}")
            continue
        n_a = con.execute(f'select count(*) from a."{t}"').fetchone()[0]
        n_b = con.execute(f'select count(*) from b."{t}"').fetchone()[0]
        if n_a != n_b:
            res.problems.append(f"{t}: row count {n_a} vs {n_b}")
        dbl = [c for c, ty in ca if ty == "DOUBLE"]
        exact = (keys or {}).get(t) or [c for c, ty in ca if ty != "DOUBLE"]
        kl = ", ".join(f'"{c}"' for c in exact)
        if exact:
            only_a = con.execute(f'select count(*) from (select {kl} from a."{t}" '
                                 f'except all select {kl} from b."{t}")').fetchone()[0]
            only_b = con.execute(f'select count(*) from (select {kl} from b."{t}" '
                                 f'except all select {kl} from a."{t}")').fetchone()[0]
            if only_a or only_b:
                res.problems.append(f"{t}: key mismatch, {only_a} rows only in baseline, "
                                    f"{only_b} only in candidate")
                continue
        for c in dbl:
            on = " and ".join(f'x."{k}" is not distinct from y."{k}"' for k in exact) or "true"
            bad = con.execute(
                f'select count(*) from a."{t}" x join b."{t}" y on {on} where '
                f'(x."{c}" is null) != (y."{c}" is null) or '
                f'abs(x."{c}" - y."{c}") > ? + ? * abs(y."{c}")', [atol, rtol]).fetchone()[0]
            if bad:
                res.problems.append(f"{t}.{c}: {bad} double values differ beyond tolerance")
    con.close()
    return res


def main(argv):
    """Compare the tables named on the command line and print the verdict.

    Args:
        argv: BASELINE, CANDIDATE, then one or more table names.

    Returns:
        0 if identical within tolerance, 1 on mismatch, 2 on too few arguments.
    """
    if len(argv) < 3:
        print(__doc__)
        return 2
    res = compare(argv[0], argv[1], argv[2:])
    for p in res.problems:
        print("MISMATCH", p)
    print("compare:", "PASS" if res.ok else "FAIL")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
