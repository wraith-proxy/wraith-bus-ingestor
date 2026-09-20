# ADR 0009: A fast nightly build -- where the time goes, and the plan to cut it

**Status:** Proposed 2026-09-20. Nothing in this ADR has been built. It records a measured profile of the nightly build, three ideas that were investigated read-only and what they showed, and the plan (workflow "WF-S") for finding and proving the change that pays. No model, test or build script has been edited.
**Date:** 2026-09-20
**Related:** [ADR-0008](0008-published-timetables-vs-inferred-routes.md) (whose speed claim this ADR corrects), [ADR-0001](0001-line-stops-memory-and-batching.md), [ADR-0006](0006-bus-positions-schema-optimisation.md), [docs/ROADMAP.md](../ROADMAP.md) (1.14.0), `dbt/build.sh`, `dbt/models/intermediate/int_pings_segmented_to_journeys.sql`

## Read this first

The owner's goal, stated plainly on 2026-09-20, is that everything done to this repository must serve one outcome: the 03:00 `dbt/build.sh` run, started by `wraith-bus-build.timer`, must **succeed** and must be **fast**. Not a one-hour job that fails one night in three. Reliability outranks speed, and both outrank tidiness. A change that deletes models but leaves the build slow has not achieved the goal, however good the deletion is.

That reframes ADR-0008. It was presented, by the assistant that wrote it, as the fix for the slow, fragile build. It is a reliability fix and a modest speed fix. The measured profile below shows that **one model, `int_pings_segmented_to_journeys`, accounts for about 85% of the dbt run**, and ADR-0008 does not touch it. The 84-model `line_stops` cluster that dominates the codebase, the ADRs and the memory-limit history costs 200 seconds. This ADR is about the model that costs 2,650.

Two findings, both measured, shape everything else:

1. The incremental model reads **four** calendar dates and rewrites **three**, because `interval '3 days'` on a `DATE` column is inclusive. The comments say three and two. The real need is about one and a bit.
2. A naive key-based journey identity, which looked like a way to delete the window machinery, was tested and **does not work**: 44% of pings sit in long-lived keys that still need splitting.

## Context: the measured profile of the nightly

Source: `dbt/build.log`, which holds several runs, and the ROADMAP's 2026-09-19 verification runs. Every figure below is read from a log line; none is estimated except where marked.

The last passing run (2026-09-19 11:00-11:13), first `dbt run` 3,131 s (52 min 11 s):

| Model | Seconds | Share of the first `dbt run` |
|---|---|---|
| `int_pings_segmented_to_journeys` | 2,649 | 85% |
| `int_pings_matched_to_stops` | 458 | 15% (ran concurrently with others; shares do not sum to 100) |
| `eta_models` | 360 | |
| `int_journeys_shaped_and_directed` | 20 | |
| `int_pings_measured_to_journey_end` | 13 | |

The second run (`--threads 1 --select int_first_visit_per_journey+`): 86 models, 200 s (3 min 20 s), which is the whole `int_line_stops_*` cluster and `line_stops`. The 884 tests: 72-97 s across runs.

`int_pings_segmented_to_journeys` in the four runs the log holds: 2,219 s, 3,154 s, 2,649 s and 2,543 s. `eta_models` in the same runs: 118 s, 6 s, 360 s, and a failure at 99 s; the variance is unexplained (the plan below has an agent chase it, and the answer may simply be what it ran concurrently with, since `--threads 3` schedules independent models together).

The full nightly, from the ROADMAP's 2026-09-19 verification: Run 1 (`SKIP_EXTRACT=1`) 1 h 02 m, Run 2 (the systemd unit, full extract) 1 h 06 m. The difference implies the extract costs on the order of four minutes (derived, not separately measured).

What that says about ADR-0008: deleting the 89 models removes `int_pings_matched_to_stops` (458 s) and the cluster (200 s), about 11 minutes of about 66. The nightly would still take about 55. It also removes the model that failed the 2026-09-20 03:00 run (OOM in `int_pings_matched_to_stops`) and three ADRs' worth of memory fragility, which is a real gain and the reason ADR-0008 stands. It is not a fast build.

## What was investigated, read-only

All of the following was measured on 2026-09-20 against the persisted build database and the extracted Parquet, opened read-only with two threads and a 2.5 GB cap, while the host was quiet. Nothing was modified.

### The incremental window is larger than the code says

The model reads `ping_local_date >= (select max(ping_local_date) from {{ this }}) - interval '3 days'` and rewrites `>= ... - interval '2 days'`. On a `DATE` column that comparison is inclusive: with the maximum date 2026-09-20 it reads 09-17, 09-18, 09-19 and 09-20. Measured on the persisted table: **27,504,425 rows over four calendar dates read, 16,439,642 rows over three dates rewritten**. The file header and the comment say three days read and two written.

What a nightly actually needs is to rewrite yesterday (closed) and today (partial at 03:00), about 8.7 million rows on the numbers above, plus a carry-in buffer. The buffer must give `lag()` its context (three or four pings back) and, more importantly, must preserve `journey_key`. That key is `bus_id || '#' || epoch_ms(start ping_ts)`, taken from a running `max()` over the bus's pings, so it is only stable if the journey's start ping lies inside what was read. A surviving journey lasts at most five hours (the duration filter in `int_journeys_shaped_and_directed`), so the theoretical buffer is about five hours and twenty minutes; the model's own comment fixes it at a whole day. Whether a shorter buffer is empirically identical is exactly what the plan measures.

### A key-based journey identity does not remove the window machinery

The hope was that since 2026-09-12 the feed carries `journey_ref` and `origin_aimed_dep`, so a journey could be identified by a key instead of six window conditions. Coverage is good: on 2026-09-13 onward, `journey_ref` is present on 67-73% of pings, `origin_aimed_dep` on 91-93.5%, and one or the other on **99.1-99.8%**. (2026-09-12 is a partial day, 71.4%, from before the field was widespread.)

But identity is not segmentation. On 2026-09-16 (13,363,753 pings), against the 1,185,008 segments the current heuristic produces, the key `(bus_id, operator, line, origin_aimed_dep or journey_ref)` gave 443,289 keys, covering 99.7% of pings. **38,282 of those keys (8.6%) span more than five hours and hold 44.1% of the keyed pings.** They are vehicles that keep advertising a stale timetable or sit at a depot, and one key holds up to 468 heuristic segments. They still need splitting, so the window logic stays. Restricting to survivors changes the picture only for the pings that matter: the 305,060 journeys that survive `int_journeys_shaped_and_directed` (4,683,517 trimmed pings; median 14 pings, 39 minutes, 9.02 km) touch 324,079 keys, only 4.9% of their pings lie in a long-lived key, and 0.09% have no key. So keys agree with the *survivors* almost one-to-one and disagree with the *junk*, which is the part that cannot be separated without the heuristic. **Not pursued**, for that reason, and recorded so it is not re-derived.

### Two-thirds of the pings are dead weight

Of 13.4 million pings on 2026-09-16, 4.68 million survive to a training journey: **65% are discarded** downstream (layovers, parked or idle buses, fragments). The windows run over all of them. That is the reason the fastest lever is not a cleverer segmentation but sending fewer, and narrower, rows through it. It is also why the wide sort payload (fifteen columns, six of them `VARCHAR`) matters: the sorts spill at `memory_limit`.

### The 2026-09-18 extract holes are source gaps

`dbt/data` holds five, four and five of the six expected four-hour files for 09-17, 09-18 and 09-19 (09-16 has all six). That looked like an extract fault. It is not: QuestDB itself has no rows for 00:00-04:00 on 2026-09-18, and the hour beginning 17:00 holds 0.23 million rows where neighbouring hours hold about 1.4 million, with nothing after. The ingestor was not writing. This is an ingest outage (the host was in trouble that day, with four OOM kills recorded), and is out of scope here; the build already tolerates holes by design.

## Decision: investigate and prove before changing the model

The model is the most expensive, the most subtle and the most historically fragile in the project: its own comments record a duplicate-`ping_seq` incident and two host OOMs from window-function formulations. Changing it on the strength of reasoning alone would be careless. So the decision is to run the investigation as a workflow, "WF-S", that changes **nothing in the repository**, and to implement only what it proves.

### The equivalence bar

A prototype must reproduce the current model's output **bit-for-bit** on the rows it writes, on a closed day, on a day with midnight-crossing journeys (night buses in particular), and at the edges of the loaded range. Where a difference is unavoidable it must be enumerated, counted, explained, and shown not to change the downstream surviving-journey set and the fitted `eta_models` coefficients. "Close" is not a result. `journey_key` must be identical whether a journey is built in a cold full build, a first incremental run, or a later incremental run with a shorter read window.

### Reliability outranks speed, and measures both

WF-S measures each candidate at `memory_limit` 6 GB (production today) and at 4 GB, where the 03:00 nightly has repeatedly died, and reports peak resident memory and spill. If the change makes the model fit comfortably at 4 GB or below, that is worth more than the time it saves, and it is reported as such.

### The plan (WF-S)

Eleven agents in six phases, Sonnet at medium effort throughout, with a single-file or scratch-only scope for each and no decisions delegated.

1. **Harness.** Freeze the inputs into a scratch database by rendering the real staging model with `dbt compile` against a throwaway profile (never the repository's database). Render the current model to a standalone query, the *oracle*, and run it once cold. Export the persisted table's rows for the test day read-only and compare them with the oracle's: if a fresh rebuild disagrees with what the incremental history has accumulated, that is a correctness finding in the existing pipeline in its own right. Build a benchmark wrapper and an equivalence checker. The wrapper serialises runs with `flock`, runs at niceness 15, enforces a memory watchdog that kills a run when host `MemAvailable` falls below 3.5 GB, tries a `systemd-run --user` cgroup cap of 13000M to match production, and logs wall time and peak resident memory.
2. **Profile.** One agent profiles the oracle at production settings, at operator level: time per operator type, the number of distinct window sorts, spill, the width of the widest sort, whether the date filter prunes at scan time, and the effect of one, three and six threads. Another builds a whole-nightly stage timeline from the logs already on disk and chases the `eta_models` variance, the whole-table rescans elsewhere in the pipeline, and the fixed overhead (extract, container stop and restart) that no model change can touch.
3. **Prototype**, three independent variants, each developed in parallel on small slices and benchmarked only through the serialised wrapper: *(a) fewer rows*, a nightly-shaped window of the last two dates plus an hour-sized buffer, with a sweep of 1, 3, 6, 12 and 24 hours against a 24-hour reference and a specific test for the surviving false journey that a truncated over-five-hour segment could produce; *(b) fewer window passes*, folding `was_standing` into the first-level lags with a fourth `lag()`, giving `row_number` and the running sum one shared sort spec, and replacing the per-journey unordered aggregates with a `GROUP BY` and join; *(c) narrower rows*, sorting a narrow core and joining the payload back on `(bus_id, ping_ts)`, after verifying that pair is unique.
4. **Combine.** Stack the winners, allowing that gains interact (fewer rows shrinks the spill that narrow rows were saving), and measure at 6 GB and 4 GB, plus the cold full-rebuild path at 6 GB, which is the disaster-recovery route and must still work. Then run the rest of the pipeline on the candidate's output and compare the `eta_models` coefficients one by one against the same pipeline on the oracle.
5. **Verify**, three adversarial lenses: whether the equivalence claim is real or vacuous (does the checker notice a corrupted row, a re-keyed journey, a night bus?); whether the result survives the real nightly (cgroup accounting of page cache, the dbt incremental wrapper, the first-run and `replay` paths, a source with holes); and whether any headline number is overclaimed (cold-cache baseline against warm-cache candidate, an ignored join cost, a parent-process-only memory figure).
6. **Report.** One document with the numbers, the whole-nightly projection stage by stage, every problem the verifiers raised stated first, what was not tested, and the exact list of changes an implementation would make.

### Safety rules built into the workflow

These come from how this host has behaved, and from one hazard found while planning.

- **DuckDB file locks.** An agent holding a connection on the build database when the timer fires would make the 03:00 nightly fail at start-up. Only the harness agent touches the build database, read-only, in short scripts that exit. Everything else works from scratch copies.
- **Host freezes.** The host hard-froze on 2026-09-14 and 2026-09-19 with the cause unexplained (`memtest86+` passed on 2026-09-20). Parallel benchmarking would be both noisy and risky, so benchmarks are serialised and watched. The workflow must not run alongside another heavy job.
- **Repository untouched.** Prototypes are SQL files in a scratch directory. The implementation is a separate step.

## Consequences and order of work

If WF-S finds a variant that is bit-identical, downstream-neutral and fits at 4 GB, the follow-up ("WF-S2") edits `dbt/models/intermediate/int_pings_segmented_to_journeys.sql` and its tests: the window logic, a dbt test that guards `journey_key` stability across incremental runs, and the retention post-hook interaction. Then ADR-0008's Phase 1 (the API rewire and deletion of the 89 models) proceeds, now for what it is good at, which is removing the model that failed the 2026-09-20 nightly and about eleven minutes.

The order is: WF-S, then WF-S2, then ADR-0008's Phase 1. The alternative order (delete first) was rejected because it spends the most work on the change that helps the goal least. It is not wasted, since it stays needed for reliability, but it is not first.

An honest estimate of the upside, marked as hypothesis and to be replaced by WF-S's measurement: rows through the windows fall from about 27.5 million to about 10-11 million, and the sort width narrows; if the cost scales with either, `int_pings_segmented_to_journeys` falls from about 44 minutes toward the ten-minute range. With ADR-0008's deletions the nightly would then be on the order of 20-25 minutes. None of this is measured.

## Ceilings and what would change the plan

- If the harness finds the oracle **disagrees with the persisted table**, the existing incremental pipeline has been accumulating inconsistent history, and that becomes the first priority ahead of speed.
- If no variant is bit-identical and downstream-neutral, the fallback is the bounding-box decision (open in the ROADMAP): the segmentation model was recorded at 724 s over the old 22-day north-west window and is about 2,650 s now over a four-date window at all-GB scale (4.7x the vehicles). The windows differ, so that is a pointer to where the cost came from and not a like-for-like ratio.
- The fixed floor (extract, container stop and restart, contract gate, tests, publish) is measured by the stage profile; if it is large, it is the next target and not the model.
- `eta_models` varies from 6 s to 360 s for a reason nobody has established; if it turns out to be scheduling contention, `--threads` and model ordering are levers.

## Roadmap

Recorded in [docs/ROADMAP.md](../ROADMAP.md) as 1.14.0 (planned, not started).
