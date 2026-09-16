#!/usr/bin/env python3
"""Generates the per-line batch models for line_stops' four grain aggregations,
plus the wrapper model each grain's batches union into and the _intermediate__models.yml
entries for the new batch models.

WHY this is a generator and not 80 hand-typed files: each grain's by_grain aggregation
(approx_quantile + avg + count(*), grouped by up to 5 columns) OOM'd the host repeatedly --
confirmed measured to be a real DuckDB limitation for this aggregate shape, not a config
mistake (memory_limit properly applied still got SIGKILL'd with no graceful DuckDB error).
Group count, not row volume, is what's fatal, and `line` is part of every grain's grouping
key, so splitting strictly by `line` guarantees any two rows sharing a full group key always
land in the same batch -- each batch's aggregate is already final and correct for the groups
it contains, no cross-batch merge needed. See docs/ROADMAP.md's 1.1.1 entry for the full
incident history and the measured group counts per grain.

A single query with N UNION ALL branches was rejected: DuckDB's optimizer isn't guaranteed to
serialize independent branches (the same lesson as the earlier AS MATERIALIZED hint failure).
A dbt Python model looping over batches was rejected too: contract={'enforced': true} is a
silent no-op on Python models in the installed dbt-duckdb 1.11.0 (verified against its
installed source -- the python materialization path never calls the contract-equivalence
check). So this generates N real, separate SQL model files instead -- dbt's own DAG/ref()/
--threads 1 machinery (already proven serializing this project's 5-model cluster correctly)
extends to them automatically, with zero changes needed to build.sh's existing
`--threads 1 --select int_first_visit_per_journey+` invocation.

Generating from one template, rather than hand-typing 80 near-identical files, also closes
the one real risk a prior design review flagged: a per-file typo in the heavy-line exclusion
list silently dropping a line's data with no test catching it. That's now structurally
impossible -- every batch's exclusion list comes from the same GRAINS config below, not 80
independently-typed copies. tests/assert_line_stops_batch_coverage.sql is kept anyway, as a
runtime check against a bug in the template or this config itself, not a substitute for it.

Batch/heavy-line assignment is frozen from a real measurement (2026-09-15, see
docs/ROADMAP.md) of each grain's per-line group-count distribution -- long, evenly-spread
tail across every grain, no single line dominant (max ~1.55% of any grain's total groups),
so a plain hash(line) % N tail split is safe from a single line blowing out a batch. The top
10 heaviest lines per grain are still placed one-per-batch explicitly (Tier 1) rather than
left to hash luck, because a fixed hash can coincidentally collide two or more of them into
one batch, and at this memory margin that's a needless, near-free-to-avoid risk. Every other
line (Tier 2) falls through to hash(line) % N, which needs no maintenance as new lines
appear in the feed.

Usage: run from the dbt/ directory (paths below are relative to it):
    ../.venv/bin/python3 scripts/generate_line_stops_batches.py
Re-run whenever a grain's measured group counts or heavy-line list changes meaningfully
(rare -- the tail is long and evenly spread, per the distribution measurement above); diff
and commit the regenerated output like any other source change.
"""
import os

# Rounded UP from each grain's real measured group count (2026-09-15) divided by a ~50,000
# groups/batch target (itself a 4x margin under a 2GB/batch budget against a ~10KB/group
# floor derived from the crash telemetry -- see docs/ROADMAP.md). 16 is a uniform floor
# across the cluster for operational simplicity; star_real doubles to 32 because its raw
# requirement (ceil(1,318,657 / 50,000) = 27) exceeds the 16 floor -- next clean doubling
# above 27.
GRAINS = {
    'real_real': dict(
        n_batches=16,
        total_groups=732_266,
        operator_filter=True,
        operator_lit=None,
        day_type_lit=None,
        group_by=['operator', 'line', 'direction', 'day_type', 'atco_code'],
        heavy=['1', '7', '2', '5', '8', '6', '3', '10', '12', '4'],
        comment_grain=(
            "This grain: real operator, real day_type. Drops rows with no operator at\n"
            "-- all (everything before 2026-09-12) rather than grouping them under a\n"
            "-- literal NULL -- a NULL-operator \"real\" grain would never be looked up by\n"
            "-- anything, since a live bus either reports a real operator (and would\n"
            "-- look for that) or none (and the API skips straight to the '*' rungs)."
        ),
    ),
    'real_star': dict(
        n_batches=16,
        total_groups=542_133,
        operator_filter=True,
        operator_lit=None,
        day_type_lit="'*'",
        group_by=['operator', 'line', 'direction', 'atco_code'],
        heavy=['1', '2', '7', '5', '8', '6', '3', '4', '10', '12'],
        comment_grain=(
            "This grain: real operator, day_type pooled as '*'. Drops rows with no\n"
            "-- operator, same reasoning as int_line_stops_real_real."
        ),
    ),
    'star_real': dict(
        n_batches=32,
        total_groups=1_318_657,
        operator_filter=False,
        operator_lit="'*'",
        day_type_lit=None,
        group_by=['line', 'direction', 'day_type', 'atco_code'],
        heavy=['1', '7', '2', '5', '8', '6', '10', '12', '9', '3'],
        comment_grain=(
            "This grain: operator pooled as '*', real day_type. No operator filter --\n"
            "-- every row (with or without an operator) belongs in the pooled rungs.\n"
            "-- 32 batches, double the other three grains: this is the worst-case grain,\n"
            "-- 1,318,657 groups measured (2026-09-15), the most of any grain."
        ),
    ),
    'star_star': dict(
        n_batches=16,
        total_groups=798_774,
        operator_filter=False,
        operator_lit="'*'",
        day_type_lit="'*'",
        group_by=['line', 'direction', 'atco_code'],
        heavy=['1', '7', '2', '5', '8', '6', '9', '10', '3', '4'],
        comment_grain=(
            "This grain: both operator and day_type pooled as '*' -- the original,\n"
            "-- fully-merged behaviour, kept as the last-resort fallback."
        ),
    ),
}

MODELS_DIR = 'models/intermediate'
YML_PATH = os.path.join(MODELS_DIR, '_intermediate__models.yml')


def select_list(cfg):
    op = 'operator' if cfg['operator_lit'] is None else f"{cfg['operator_lit']} as operator"
    dt = 'day_type' if cfg['day_type_lit'] is None else f"{cfg['day_type_lit']} as day_type"
    return f"    {op},\n    line,\n    direction,\n    {dt},\n    atco_code,"


def line_predicate(cfg, i, n_batches):
    heavy_list_sql = ', '.join(f"'{h}'" for h in cfg['heavy'])
    tail = f"line not in ({heavy_list_sql}) and hash(line) % {n_batches} = {i}"
    if i < len(cfg['heavy']):
        heavy_line = cfg['heavy'][i]
        return f"line = '{heavy_line}'\n    or ( {tail} )"
    return tail


def batch_where(cfg, i, n_batches):
    pred = line_predicate(cfg, i, n_batches)
    if cfg['operator_filter']:
        return f"where operator is not null\n  and ( {pred} )"
    return f"where {pred}"


def batch_sql(grain, cfg, i):
    n = cfg['n_batches']
    heavy_note = (
        f"the {len(cfg['heavy'])} heaviest lines (measured 2026-09-15, together ~9-10% of "
        f"this grain's total groups) are placed one-per-batch by hand, heaviest first, "
        f"instead of left to hash(line) -- a fixed hash can coincidentally collide two or "
        f"more of them into the same batch, and at this memory margin that's a needless "
        f"risk for a near-free fix. Every other line (the long, evenly-spread tail) is "
        f"bucketed by hash(line) % {n}, which needs no maintenance as new lines appear."
    )
    owns = (
        f"This batch ({i} of {n}) owns line '{cfg['heavy'][i]}' plus its 1/{n} share of the tail."
        if i < len(cfg['heavy'])
        else f"This batch ({i} of {n}) holds no dedicated heavy line -- tail only, "
        f"via hash(line) % {n} = {i}."
    )
    return f"""{{{{ config(materialized='table', contract={{'enforced': true}}) }}}}

-- Batch {i} of {n} of int_line_stops_{grain} -- generated by
-- dbt/scripts/generate_line_stops_batches.py, do not hand-edit. See that
-- script's module docstring for the full OOM/batching-by-line rationale,
-- shared across all four grains and every one of their batches.
--
-- {cfg['comment_grain']}
--
-- Bucketing: {heavy_note}
-- {owns}
--
-- Column list/types are enforced by the dbt contract above -- same
-- contract as every other batch and the 3 sibling grains' batches, so
-- everything stays UNION-ALL-compatible with a compile-time error instead
-- of a silent runtime mismatch if one of them ever drifts. Drift across
-- batches is also structurally prevented by generation from one template
-- and one config (see the script) rather than hand-typed duplication.
select
{select_list(cfg)}
    cast(approx_quantile(secs_from_origin, 0.5) as double) as median_secs_from_origin,
    -- Only ever used to break ties in the ordering, never published.
    -- Pings are 3-minute buckets, so secs_from_origin is quantised to
    -- multiples of 180 and a run of consecutive stops shares one median.
    -- The mean is continuous and separates them in the right order.
    avg(secs_from_origin)                                  as mean_secs_from_origin,
    -- count(*), not count(distinct journey_key): int_first_visit_per_journey's
    -- own GROUP BY already guarantees journey_key can't repeat within a
    -- group here -- see int_line_stops_{grain}'s own header (the wrapper
    -- this batch feeds) for the full reasoning.
    cast(count(*) as bigint)                               as n_obs
from {{{{ ref('int_first_visit_per_journey') }}}}
{batch_where(cfg, i, cfg['n_batches'])}
group by {', '.join(cfg['group_by'])}
"""


def wrapper_sql(grain, cfg):
    n = cfg['n_batches']
    refs = '\n'.join(
        f"    select * from {{{{ ref('int_line_stops_{grain}_b{i}') }}}}"
        + ('' if i == n - 1 else '\n    union all')
        for i in range(n)
    )
    return f"""{{{{ config(materialized='table', contract={{'enforced': true}}) }}}}

-- Thin union of the {n} per-line batches that actually do this grain's
-- aggregation (int_line_stops_{grain}_b0.sql .. _b{n - 1}.sql, generated by
-- dbt/scripts/generate_line_stops_batches.py). Batching by `line` -- part
-- of every grain's GROUP BY key -- fixes a repeated full-host OOM (see
-- docs/ROADMAP.md's 1.1.1 entry) without changing this model's output:
-- `line` being part of the key means two rows sharing a full group key
-- always land in the same batch, so each batch's aggregate is already
-- final for the groups it contains and this union needs no
-- re-aggregation.
--
-- Kept under this exact model name so every downstream ref() -- this
-- grain's three siblings and line_stops.sql's own union -- needs zero
-- changes. Still contract-enforced so a batch drifting from the 8-column
-- shape is still a compile-time error, not a silent one.
--
-- {cfg['comment_grain']}
select * from (
{refs}
)
"""


def yml_entry(grain, cfg, i):
    n = cfg['n_batches']
    op_desc = (
        "National Operator Code. Never NULL here -- this grain filters those rows out."
        if cfg['operator_lit'] is None
        else "Always the literal '*' in this grain."
    )
    op_tests = (
        "        tests: [not_null]"
        if cfg['operator_lit'] is None
        else (
            "        tests:\n          - not_null\n          - accepted_values:\n"
            "              arguments:\n                values: ['*']"
        )
    )
    dt_desc = (
        "Service pattern of the day."
        if cfg['day_type_lit'] is None
        else "Always the literal '*' in this grain."
    )
    dt_tests = (
        "        tests: [not_null]"
        if cfg['day_type_lit'] is None
        else (
            "        tests:\n          - not_null\n          - accepted_values:\n"
            "              arguments:\n                values: ['*']"
        )
    )
    return f"""  - name: int_line_stops_{grain}_b{i}
    description: >
      Batch {i} of {n} of int_line_stops_{grain}, split by line to keep group
      count -- not row volume -- from OOM'ing approx_quantile's per-group
      state. Generated by dbt/scripts/generate_line_stops_batches.py. See
      models/intermediate/int_line_stops_{grain}.sql (the union wrapper this
      batch feeds) and docs/ROADMAP.md's 1.1.1 entry.
    columns:
      - name: operator
        data_type: varchar
        description: {op_desc}
{op_tests}
      - name: line
        data_type: varchar
        description: Service number.
        tests: [not_null]
      - name: direction
        data_type: varchar
        description: Journey direction.
        tests: [not_null]
      - name: day_type
        data_type: varchar
        description: {dt_desc}
{dt_tests}
      - name: atco_code
        data_type: varchar
        description: The stop.
        tests: [not_null]
      - name: median_secs_from_origin
        data_type: double
        description: Median seconds from journey origin to this stop, over every journey in this batch (approx_quantile, bounded t-digest).
        tests: [not_null]
      - name: mean_secs_from_origin
        data_type: double
        description: Mean seconds from journey origin to this stop. Tie-break only, never published.
        tests: [not_null]
      - name: n_obs
        data_type: bigint
        description: Distinct journeys observed at this stop in this batch.
        tests: [not_null]
"""


def main():
    yml_blocks = []
    for grain, cfg in GRAINS.items():
        n = cfg['n_batches']
        for i in range(n):
            path = os.path.join(MODELS_DIR, f'int_line_stops_{grain}_b{i}.sql')
            with open(path, 'w') as f:
                f.write(batch_sql(grain, cfg, i))
            yml_blocks.append(yml_entry(grain, cfg, i))

        wrapper_path = os.path.join(MODELS_DIR, f'int_line_stops_{grain}.sql')
        with open(wrapper_path, 'w') as f:
            f.write(wrapper_sql(grain, cfg))

        print(f'{grain}: wrote {n} batches + wrapper')

    yml_out = os.path.join(MODELS_DIR, '_generated_line_stops_batches.yml')
    with open(yml_out, 'w') as f:
        f.write('version: 2\n\nmodels:\n')
        f.write('\n'.join(yml_blocks))
    total = sum(cfg['n_batches'] for cfg in GRAINS.values())
    print(
        f'wrote {total} batch model files + 4 wrapper files + '
        f'{yml_out} ({len(yml_blocks)} yml entries)'
    )


if __name__ == '__main__':
    main()
