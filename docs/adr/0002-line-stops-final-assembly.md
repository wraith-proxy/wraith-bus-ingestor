# ADR 0002: line_stops final assembly — stop_identity promotion and mode() → any_value()

**Status:** Accepted
**Date:** 2026-09-15
**Related:** [ADR-0001](0001-line-stops-memory-and-batching.md), [docs/ROADMAP.md § 1.1.1](../ROADMAP.md)

## Context

After ADR-0001's batching fix, the first real `dbt run` of the complete
86-model cluster was executed — not another isolated test, the actual
production invocation `build.sh` uses. 85 of 86 models completed cleanly
and fast (0.4–2.4s each). **The 86th — `line_stops` itself — still got
OOM-killed** (peak RSS 4.48GB, no graceful DuckDB error). Nobody had been
able to observe this failure mode before: on every prior attempt, all the
way back to the original crash, the query died further upstream before
`line_stops` ever got the chance to run.

## Investigation

Two independent subtrees fed `line_stops`' final join:

- The `by_grain`/window branch — already proven cheap by ADR-0001's work
  (1.04M rows post-filter, max partition 2,490 rows).
- `stop_identity` — `mode(stop_name)`, `mode(locality)`, `avg(lat)`,
  `avg(lon)` grouped by `atco_code`, over the raw 8.75M-row
  `int_pings_matched_to_stops` table.

Both measured cheap in isolated read-only queries beforehand. But those
measurements checked the *input* — row counts, group counts,
distinct-values-per-group — never the actual materialized aggregation.

Given every lesson already established in ADR-0001 about DuckDB not
reliably serializing independent branches within one query, the working
theory was that these two branches ran concurrently and their *combined*
memory exceeded budget. Per explicit direction, the confirmatory diagnostic
(`EXPLAIN ANALYZE` proving or disproving concurrent execution) was skipped
in favour of applying the structurally-correct fix directly: promoting
`stop_identity` to its own dbt model, giving `line_stops` a real `ref()`
dependency instead of an in-query subtree.

**That fix was necessary, but it wasn't sufficient.** Verified under the
same capped discipline as every test in ADR-0001: `int_stop_identity`,
built completely alone with nothing else competing, still got SIGKILL'd —
2.19GB, in roughly 1–2 seconds of actual execution. This falsified the
concurrency theory outright: the real cost was `mode()` itself, not two
branches racing. `mode()` carries the same "per-group aggregate state that
`memory_limit` doesn't reliably bound" limitation ADR-0001 already found on
`approx_quantile` — a second, independent instance of the same underlying
DuckDB behavior, surfacing in a completely different aggregate function.

## Decision

1. **Promote `stop_identity` to its own dbt model** (`int_stop_identity`),
   kept even though it alone didn't fix the OOM — it's still the
   structurally correct move (a real DAG dependency, matching every other
   model in this project) and it's precisely what made the second problem
   diagnosable at all: isolating it let it be tested alone, which is how
   the concurrency theory got falsified instead of staying an unverified
   assumption.
2. **Replace `mode(stop_name)`/`mode(locality)` with
   `any_value(stop_name)`/`any_value(locality)`.** Checked before applying,
   not assumed: measured 0 of 173,639 stops (2026-09-15) have more than one
   distinct `stop_name` or `locality` across all their raw pings —
   `mode()` and `any_value()` are therefore provably identical on this
   data. `any_value()` does no frequency-tracking, sidestepping whatever
   per-group state `mode()` was building.
3. **Add a guard test**,
   `tests/assert_stop_identity_names_are_single_valued.sql`. Unlike the
   `count(*)` fix in ADR-0001 (a structural guarantee, true by
   construction, forever), this one is an **empirical fact about current
   data**. If NaPTAN ever genuinely renames a stop mid-window, or
   ingestion introduces name drift, `any_value()` would silently start
   returning an arbitrary pick instead of the most common one — the guard
   test catches that instead of letting it ship quietly.

## Measured data

| step | result |
|---|---|
| Full 86-model cluster, first real `dbt run` | 85/86 OK, `line_stops` killed at 4.48GB |
| `int_stop_identity` alone, `mode()`, capped | Killed at 2.19GB in ~1–2s, zero competing branches |
| `int_stop_identity` + `line_stops`, `any_value()` fix, capped | **Completed: 8s wall, 907MB peak RSS** |
| Full relevant test suite | 786/786 PASS, including the new guard test |

```
Peak RSS: mode() alone vs any_value() end-to-end
mode() (int_stop_identity only, no competing branch)  ██████████████████████████  2.19 GB (killed)
any_value() (int_stop_identity + line_stops, both)     ██████                      0.91 GB (completed)
```

## A note on methodology

This is the second time in this investigation that "the input looks cheap"
was mistaken for "the aggregation is cheap." Checking group counts and
distinct-value distributions measures the *shape* of the data, not the
cost of the specific aggregate function computing over it. The only test
that actually answers "does this OOM" is running the real aggregation,
capped, and watching what happens — every cheaper proxy tried in this
project has eventually been wrong at least once.

## Consequences

**Cost:** one more model in the cluster (97 total, up from 96) and one
more singular test.

**Benefit:** `line_stops` itself now builds in 2.36s, verified under the
exact production-shape invocation, not just isolated component tests.
ADR-0001's "no real, full `dbt run` of the complete cluster" open item is
now closed.

## Open items

- ADR-0001's other open items (cold-cache validation, `ops/systemd/`
  deployment, route-reliability re-measurement) still stand — this ADR
  only closes the "no real full `dbt run`" item.
- Not yet done: a systematic check for other single-model aggregations in
  this project using `mode()`, `approx_quantile()`, or similar
  possibly-unbounded-per-group functions that have only ever been assumed
  cheap from input-shape checks, never measured under real execution. This
  investigation found two such cases by accident, not by deliberate review.

## References

- [ADR-0001](0001-line-stops-memory-and-batching.md) — the batching fix
  this ADR builds on
- Commit `9b58e6b`
- `dbt/models/intermediate/int_stop_identity.sql`
- `tests/assert_stop_identity_names_are_single_valued.sql`
