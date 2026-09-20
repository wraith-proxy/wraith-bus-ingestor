# ADR 0009: A fast nightly build -- where the time goes, and the plan to cut it

**Status:** Implemented 2026-09-20 (supervised migration run and publish the same night; not yet observed in a real 03:00 run). This ADR began as a proposal and a plan; the sections below are in the order they were written, and the last one, "Amendment: what was deployed", is the current state and supersedes any "not applied" statement above it. It records a measured profile of the nightly build, ideas that were investigated and what they showed, the window fix, and the deterministic segmentation change.
**Date:** 2026-09-20
**Related:** [ADR-0008](0008-published-timetables-vs-inferred-routes.md) (whose speed claim this ADR corrects), [ADR-0001](0001-line-stops-memory-and-batching.md), [ADR-0006](0006-bus-positions-schema-optimisation.md), [docs/ROADMAP.md](../ROADMAP.md) (1.14.0), `dbt/build.sh`, `dbt/models/intermediate/int_pings_segmented_to_journeys.sql`

## Read this first

The owner's goal, stated plainly on 2026-09-20, is that everything done to this repository must serve one outcome: the 03:00 `dbt/build.sh` run, started by `wraith-bus-build.timer`, must **succeed** and must be **fast**. Not a one-hour job that fails one night in three. Reliability outranks speed, and both outrank tidiness. A change that deletes models but leaves the build slow has not achieved the goal, however good the deletion is.

That reframes ADR-0008. It was presented, by the assistant that wrote it, as the fix for the slow, fragile build. It is a reliability fix and a modest speed fix. The measured profile below shows that **one model, `int_pings_segmented_to_journeys`, accounts for about 85% of the dbt run**, and ADR-0008 does not touch it. The 84-model `line_stops` cluster that dominates the codebase, the ADRs and the memory-limit history costs 200 seconds. This ADR is about the model that costs 2,650.

Two findings, both measured, shape everything else:

1. The incremental model reads **five** calendar dates (40.9 million rows) and rewrites **four** (27.5 million) on a real nightly, because `interval '3 days'` on a `DATE` column is inclusive and because `max(ping_local_date)` at run start is the *previous* night's date. The comments say three days read and two rewritten. The real need is to rewrite two dates (8.7 million rows) and read them plus a one-day buffer (16.4 million).
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

The model reads `ping_local_date >= (select max(ping_local_date) from {{ this }}) - interval '3 days'` and rewrites `>= ... - interval '2 days'`. On a `DATE` column that comparison is inclusive, and `max(...)` is evaluated when the run *starts*, which is the previous night's date. On the 2026-09-20 03:00 run the maximum was 2026-09-19, so it read 09-16 to 09-20 and rewrote 09-17 to 09-20. Measured on the persisted table: **40,868,178 rows over five calendar dates read, 27,504,425 rows over four dates rewritten**. The file header and the comment say three days read and two written. (An earlier version of this ADR quoted 27.5 million as the *read* and 16.4 million as the rewrite; that took the maximum after the run instead of before it, and understated the over-read.)

What a nightly actually needs is to rewrite the previous night's date (which was only partial when it last ran) and today's (partial at 03:00), `ping_local_date >= max(this)`: 8,718,037 rows on the same numbers, read with a carry-in buffer of 16,439,642 rows at one day (`>= max - 1 day`). The buffer must give `lag()` its context (three or four pings back) and, more importantly, must preserve `journey_key`. That key is `bus_id || '#' || epoch_ms(start ping_ts)`, taken from a running `max()` over the bus's pings, so it is only stable if the journey's start ping lies inside what was read. A surviving journey lasts at most five hours (the duration filter in `int_journeys_shaped_and_directed`), so the theoretical buffer is about five hours and twenty minutes; the model's own comment fixes it at a whole day. Whether a shorter buffer is empirically identical is exactly what the plan measures.

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

An honest estimate of the upside, marked as hypothesis and to be replaced by WF-S's measurement: rows through the windows fall from about 40.9 million to about 16.4 million with a one-day buffer, and lower still with an hour-sized one, and the sort width narrows; if the cost scales with either, `int_pings_segmented_to_journeys` falls from about 44 minutes toward the ten-minute range. With ADR-0008's deletions the nightly would then be on the order of 20-25 minutes. None of this is measured.

### Finding: the segmentation model is not deterministic, because `bus_id` is not a vehicle

Found by the WF-2 test rig (2026-09-20) and traced the same day. The rig could not reproduce the persisted table bit-for-bit even with the *unchanged* model, because `int_pings_segmented_to_journeys` partitions every window by `bus_id` and orders by `ping_ts` with no tiebreak, and `bus_id` is not unique across operators. The model's own header says so ("operators mint their own vehicle refs, so bus_id '3104' is fifteen different physical buses in fifteen different towns") and papers over it with a 120 km/h speed break, an operator-change break and a line-change break. Measured on the 2026-09-16 extract (13,365,206 rows):

- 12,542,928 distinct `(bus_id, timestamp)` keys, so **822,278 surplus rows (6.2%)** share a key with another row. Sample: bus `154` at the same instant is line 37 (SWWD, Hampshire), line 11 (TFCN, Cornwall) and line Jo (GPLM, near Luton).
- 27,063 distinct `bus_id` but only 28,836 distinct `(operator, bus_id)` (1.07x); 1,382 ids (5.1%) are used by more than one operator.
- Keyed by `(operator, bus_id, timestamp)` the surplus falls to **49,295 rows (0.37%)**, and what remains is one vehicle reporting two lines inside a single 3-minute bucket (for example a TfL bus straddling a line change), which a deterministic tiebreak can order.

Consequences: interleaved unrelated vehicles are cut into fragments (1,185,008 heuristic segments on that day against 443,289 keys), tied rows are ordered arbitrarily so a bus's journey split can change run to run, and, because the old window rewrote three older dates every night, those splits were re-rolled nightly. It also means "bit-identical to the persisted table" is an impossible bar for any change to this model, which is why WF-2's equivalence run could only be made on a *stable scope* (non-tied buses, journeys closed before the replay cutoff): there the new window matched persisted on 2,287,232 rows and 129,606 surviving journeys with zero duplicate `(journey_key, ping_seq)`, and the unchanged control shows differences of the same kind elsewhere.

**Since built, proven in scratch on a cold build, and then deployed with corrections:** see "Amendment: the deterministic key" for the scratch proof and "Amendment: what was deployed" for what reached the repository.

## Ceilings and what would change the plan

- If the harness finds the oracle **disagrees with the persisted table**, the existing incremental pipeline has been accumulating inconsistent history, and that becomes the first priority ahead of speed.
- If no variant is bit-identical and downstream-neutral, the fallback is the bounding-box decision (open in the ROADMAP): the segmentation model was recorded at 724 s over the old 22-day north-west window and is about 2,650 s now over a four-date window at all-GB scale (4.7x the vehicles). The windows differ, so that is a pointer to where the cost came from and not a like-for-like ratio.
- The fixed floor (extract, container stop and restart, contract gate, tests, publish) is measured by the stage profile; if it is large, it is the next target and not the model.
- `eta_models` varies from 6 s to 360 s for a reason nobody has established; if it turns out to be scheduling contention, `--threads` and model ordering are levers.

## Amendment: the window fix

*Written 2026-09-20, after a workflow ("stream A") tried to prove a narrower window. This section corrects the "not started" wording above. Nothing here has run in the real nightly.*

### Outcome, stated first

*(Superseded: see "Amendment: what was deployed".)* **The window fix was not applied at the time this section was written.** The stream halted with no variant proven, and the repository was not edited: `dbt/models/intermediate/int_pings_segmented_to_journeys.sql` still reads `>= max(this) - interval '3 days'` and rewrites `>= max(this) - interval '2 days'`. The next 03:00 nightly therefore behaves exactly as before, reading about 40.9 million rows and rewriting about 27.5 million. Nothing in this section is in the working tree or in a commit, and nothing has been tested inside the real service.

The reason it halted is a shortage of like-for-like evidence, not a failed test. The proof needs the same night replayed twice, once with the unchanged model (the control) and once with the candidate, and the two compared. Only half of each pair was run: the candidate for night 2026-09-17 and the control for night 2026-09-19. The candidate on 09-19 was skipped because the clock had passed the 00:30 UTC cut-off the workflow set for starting heavy runs, which exists to keep them clear of the 01:30-05:00 nightly window. The 09-19 control had itself been started at 15:29 UTC, on the reading that the cut-off protects the nightly window, so the skip was inconsistent; the next window was about nine and a half hours away, so the candidate run could have finished. A control on 09-17 was never in the run list, so it was not skipped, just absent. It is recorded here as a judgement that cost the proof, not as a technical obstacle.

### What was tested and what it showed

The candidate ("v24") changes two constants: read `>= max(this) - interval '1 day'`, rewrite `>= max(this)`. Because `ping_local_date` is a `DATE`, both comparisons are inclusive, so this reads the previous night's date, that date's predecessor, and the partial current date, and rewrites the previous night's date and the current one. It also corrects the header comments, which had claimed three days read and two rewritten. Two shorter-buffer variants (`vh6`, `vh12`) cut the carry-in day to 6 or 12 hours before local midnight; their daylight-saving arithmetic was proven (the exact SQL expression gave the right instant on 2026-03-28, 03-29, 03-30, 10-24, 10-25 and 10-26 and on a summer and a winter date, and a synthetic filter over the fall-back day kept the expected first ping), but neither was run against real data.

Measured, each on its own night (a scratch replay of the nightly on a copy of the build database, niced and serialised, at the production `memory_limit` and thread count under a 13,000 MB cgroup cap):

| Run | Night | Rows read | Rows written | Wall (segmentation) | Peak RSS |
|---|---|---|---|---|---|
| Control (unchanged model) | 2026-09-19 | 45,188,214 | 32,150,141 | 942.1 s | 6,575 MB |
| v24 | 2026-09-17 | 29,148,554 | 16,110,481 | 179.3 s | 6,491 MB |

**These two rows are not a comparison.** They are different nights with different data volumes, and night 09-19 has an empty partial day because the source has no 09-19 `_00` chunk (an ingest outage). Reading 179 s against 942 s as a speed-up would be overclaiming. What the table does show is that the candidate's read and write counts are what the reasoning predicted for a one-day buffer, and that peak memory is unchanged at about 6.5 GB, so the change is a speed change and not a memory fix. The rows-read and rows-written figures come from the compiled model with `max(this)` pinned to the value the run started from; dbt does not report them. On the 09-17 candidate run `int_journeys_shaped_and_directed` took 22.1 s at 3,726 MB.

### Equivalence evidence, and why it is partial

Two noise sources make any replay differ from the persisted rows, and both must be understood before reading a comparison.

1. **Tie noise.** On 2026-09-16, 20,971 of 27,079 buses have at least one duplicated `(bus_id, ping_ts)`, and on 09-18, 20,825 of 28,904. The model orders by `ping_ts` with no tiebreak, so how a tied bus's pings are split into journeys changes from run to run. A replay can never match the persisted rows for those buses, and a control would differ from another control in the same way. This is a finding about the existing model in its own right: its output is not deterministic for most buses. It is the reason ADR-0008 needs a position-based tiebreak for a different purpose, and the two should not be confused.
2. **Replay truncation.** A replay ends at the night's `_00` chunk (last ping about 03:57 UTC), whereas the persisted rows were finalised by later nights. Journeys still open at the cutoff, such as overnight depot standers, differ in end time, ping count and cumulative distance.

The comparison tool therefore reports each check on three scopes: all buses; non-tied buses; and a stable scope (non-tied, journey closed before the cutoff, persisted reference only). Cumulative and path distances are compared to six decimal places because the parallel float sum varies by about 2e-14 between runs. Results:

- **v24, night 09-17, against the persisted 09-16 rows.** Stable scope: 2,287,232 rows identical (zero rows in either direction of an `EXCEPT ALL`), 129,606 surviving journeys identical, no duplicate `(journey_key, ping_seq)`. On all buses, 605,512 journey keys differ (44,683 of them surviving journeys); on non-tied buses, 4,603 differ (3,646 surviving). The differing columns are journey end time and ping count, plus the first and last moving sequence on some rows; on the stable scope no row lacks a partner on `(journey_key, ping_seq)`, but on the all-buses scope 493,231 rows per side have no partner.
- **Control, night 09-19, against the persisted 09-18 rows.** Row counts match (7,721,605 each). All buses: 1,188,858 rows differ in each direction, 344,223 journey keys involved; 19,050 differing keys are surviving journeys. Non-tied buses (1,995,532 rows) and the stable scope (1,710,223 rows): zero difference in both directions, and the survivor sets are identical (106,578 and 105,176 journeys).

The control result matters most. The **unchanged model, replayed on 09-19, is identical to the persisted rows on the stable scope and differs elsewhere** (all-buses `EXCEPT ALL` 1,188,858 of 7,721,605 rows, about 15%; 267,436 rows per side with no partner on `(journey_key, ping_seq)`), as the candidate does on 09-17 (4,255,863 of 13,363,753, about 32%). These are different nights with different magnitudes, so the degree is not comparable; only the stable-scope result is common to both. It is therefore not evidence that the candidate did not cause the differences. It is not the proof, because the proof is a control and a candidate on the *same* night compared with each other (`--ref-label control`), which was not done. The stable scope is about 17% of rows on 09-16, so "identical" covers a minority of the data; the rest is unproven, not shown equal. The strict-scope result also says nothing about journeys whose start ping falls before a shortened buffer, which is why the shorter variants need their own run.

### Why the one-day buffer, and why nothing smaller was tried

A surviving journey lasts at most five hours (the duration filter in `int_journeys_shaped_and_directed`), and `journey_key` is `bus_id || '#' || epoch_ms(start ping_ts)`, taken from a running maximum over the pings read. The key is only stable if the start ping lies inside what was read. A buffer of three hours would cut journeys that started earlier than three hours before the window and give them a different key and a truncated start: **unsafe by construction**, so it was not built or tested. Six and twelve hours cover the five-hour limit with a margin, and were built for the sake of a further saving, but the stream stopped before running them. The whole-day buffer (v24) is the conservative choice, is the one that was run, and is what the model's own comment already fixes; it takes rows read from five dates to about three and rows written from four dates to two, and it was preferred over a smaller buffer because reliability outranks speed and the extra day costs little next to removing two dates.

### The trade-off, so it is not discovered later

Rewriting only `>= max(this)` means **late-arriving data for a date older than the previous night's is no longer re-segmented nightly**. Today, with four dates rewritten, a ping that reaches the extract two days late still lands in a rewritten date and is healed on that or the next night. Under v24 it is not. In practice the extract is written from QuestDB after the fact and the retention window is eight days, so late data arises from a backfill or a restored day (`ops/incident.py`, which runs with `REPLAY=1`). When late data is suspected, `FULL_REFRESH=1 ./build.sh` recomputes the whole window; see [docs/operations.md](../operations.md). This is a real loss of a safety margin and it is accepted only if the owner agrees that the saving justifies it.

### Verification

No independent adversarial verification of stream A was reported. The findings above are the harness's own caveats. The workflow's plan called for three adversarial verifiers; for the window fix that step did not happen.

### What the owner must do, and how to roll back

To finish the proof, run the two missing nights with the rig kept in the session scratchpad (`wf2/night.sh`, `compare.py`, `bench.sh`, described in its `RIG.md`; it lives under `/tmp` and is not in the repository, so copy it if it is to outlive the session): candidate on 2026-09-19 and control on 2026-09-17, each about 40 minutes of which about 17 is the base-database copy, outside the 01:30-05:00 window, then `compare.py --ref-label control` on each night. If both are identical on all scopes except tie and truncation noise, apply the two-constant change to the model (a single commit) and let the next nightly exercise it. **That has not happened.** Once a change is applied it takes effect at the next 03:00 nightly and is **unproven in the real service** until that run finishes; watch that first run. Rolling back is a `git revert` of that commit; the persisted table needs no repair, since the window only chooses which dates are rewritten, and `FULL_REFRESH=1` rebuilds it if in doubt.

### What remains of the plan

The status above ("Nothing in this ADR has been built") stands for the model. What changed is the evidence: the window over-read is confirmed by a replay (the unchanged model read 45.2 million rows and rewrote 32.2 million on a night with an empty partial day), the noise sources are identified and a comparison tool that copes with them exists, and the daylight-saving arithmetic for the shorter buffers is proven. The fewer-window-passes and narrower-rows variants, the 4 GB measurements, the whole-nightly projection and the verification phase have **not** been done. The `eta_models` variance is still unexplained.

## Amendment: the deterministic key

*Written 2026-09-20, before deployment; see "Amendment: what was deployed" for the outcome. This records a workflow that built the operator-keyed model in scratch and measured it. **At that point the change was NOT applied**: `extract.py`, `stg_bods__vehicle_pings.sql`, `int_pings_segmented_to_journeys.sql` (beyond the window fix already committed) and `build.sh` are unchanged in the repository, and the next 03:00 run behaves as it did before this amendment. Everything below was run in a scratch copy of the project against scratch DuckDB files, never in the real service.*

### Outcome, stated first

The design (D1 to D5) was proven for what it was designed to fix, determinism on a cold build, and the workflow did not apply it because the workflow's Apply step did not run and the verifiers found unresolved gaps. The owner's instruction was to apply it after a passing dry run. The main gap: **the windowed (03:00) path does not equal a cold build**. On the night simulations the incremental run leaves the previous local date with truncated journey ends (below). The verifiers also found that the planned dry run would consume the one-off migration on old-shape data, and that the D5 test as prototyped is too expensive to sit in the nightly. None of these is a flaw in the operator key itself; they are gaps in the surrounding plan, and each has a stated remedy.

### What was designed

- **D1, `dbt/extract.py`:** `KEYS = ["timestamp","bus_id","operator","line"]`, `operator` removed from `WANTED`, and `recorded_at` (the feed's per-record observation time, epoch milliseconds, typed `BIGINT`) added to `WANTED`.
- **D2, staging:** `recorded_at` exposed as an optional column exactly like the existing optional ones (typed `NULL` when absent from the parquet union, so old files still build).
- **D3, the model:** every window partitions by `(coalesce(operator,'~none~'), bus_id)` and orders by `(ping_ts, recorded_at, line, lat, lon)`, a total order. `journey_key` becomes `coalesce(operator,'~none~') || '|' || bus_id || '#' || epoch_ms(start ping_ts)`. The operator-change journey break and its `lag()` are removed, since an operator can no longer change inside a partition.
- **D4, `dbt/build.sh`:** before the first dbt run, if the build database exists and `int_pings_segmented_to_journeys` holds any `journey_key` not matching `'%|%#%'`, print "==> legacy journey_key shape detected: forcing FULL_REFRESH=1 (one-off)" and set `FULL_REFRESH=1`, using the venv Python and DuckDB read-only in a process that exits before dbt starts.
- **D5, tests:** keep `assert_segmented_ping_seq_unique`; add `assert_segmented_order_is_total`.

The prototype of D4's detector passed every isolation case the verifier ran (legacy keys, all-old, all-new, empty table, absent table, absent file, file held open by another process, leftover WAL, corrupt file; a writable connection succeeded afterwards each time, so no lock survives into dbt). It silently skips when the file is locked or corrupt, which is safe but should print a warning.

### The extract fix: rows the old extract merged

`extract.py` builds each four-hour chunk with QuestDB `SAMPLE BY 3m` and groups on the non-aggregated columns. `operator` was not among them, so two vehicles of different operators sharing a `bus_id` and a `line` inside one bucket were silently merged into one row: data loss at the source. Over the five days 2026-09-13 to 09-17 the old extract merged 28,927 rows (0.052%); the new extract has 55,449,341 rows against the old 55,420,414. In the one chunk examined (2026-09-16 08:00 to 12:00 UTC: 2,258,913 rows old, 2,260,013 new) all 1,100 merged groups were across different operators. The new query cost about 0.45 GiB more QuestDB memory in that chunk test; 16.5 s old against 15.8 s new.

### Does `recorded_at` reach 0%? The exact answer

Yes, with a caveat about what "yes" means. Over the five days (55,449,341 rows) the `(operator, bus_id, timestamp)` key has 55,252,475 distinct values, so **196,866 surplus rows (0.355%) in 196,748 groups**; in every group the same vehicle reports two or more lines inside one three-minute bucket. `recorded_at` is distinct within all 196,748 groups, so keying on `(operator, bus_id, timestamp, recorded_at)` leaves **0 ties**, and the full sort key leaves 0 as well (55,449,341 groups for 55,449,341 rows, so not even a true duplicate survives). `recorded_at` is never null in this extract and no `bus_id` contains `|`. The old `(bus_id, timestamp)` key had 3,264,579 surplus rows over the same days. (The 0.37% quoted earlier is the single day 2026-09-16; 0.355% is the five-day figure.)

The caveat, which the verifiers pressed and which is right: `extract.py` groups on `line`, so `(operator, bus_id, timestamp, line)` is unique by construction whatever the data does, and line alone would also reach 0. `recorded_at` is therefore **not needed for uniqueness**. What it adds is the true chronological order of the two lines' pings, where ordering by line would be total but arbitrary. It is also often stale as a clock (27% of a 09-16 sample lag the bucket start by more than five minutes, 15% by more than an hour, up to a day; the median of timestamp minus `recorded_at` is -41 s), which is why it is a tiebreak and nothing more.

### The determinism proof

Two cold builds of each model over the same five-day data (through a flock-serialised, niced wrapper under a cgroup cap):

- **Old model:** 55,420,414 rows both times, but **9,294,773 rows (16.8%) in run 1 are not in run 2, and the same number the other way**. On a 2,260,013-row slice the old model differed between its own runs on 304,196 rows (13.5%). The old model does not reproduce itself.
- **New model:** the second cold run is identical to the first: 0 rows in either direction, 0 duplicate `(journey_key, ping_seq)`, and the same on the slice.

Caveats: the comparison of the full runs used a 64-bit per-row hash over all columns (floats rounded to six decimal places) because a column-by-column `EXCEPT ALL` on 55 million rows spilled and was abandoned after more than an hour; the chance of a hash collision hiding a difference is about 1e-4 at that size. Floats are therefore identical to six decimals, not bit-identical (`cum_km` differs at exact equality on some two million rows by up to 8.5e-14). The hash script prints DIFFERENT but exits 0, so it must not be wrapped in a script that trusts the exit code. Old and new used different extracts, so their row counts differ by the merged rows, which is not a determinism effect. The proof covers five days, not the eight-day window.

### The windowed-equals-cold simulation: what passed and what did not

The night simulation builds the state a real night would find (the cold reference for local dates up to the previous maximum), runs the model over the data a night would have (UTC days up to N-1) with the window fix, and compares the rewritten dates with the cold reference. A date is comparable only if it ended at least six hours before the last ping in both data sets.

- **Night N = 2026-09-16 (state 31,008,923 rows, 13,046,399 replaced; 23,833,565 read, 13,595,422 written; 86 s of dbt):** no rewritten date was comparable, because the data ends 23:57 UTC and local 09-15 ends at 23:00 UTC. On a hand-made subset (journeys closed before 23:37 UTC on 09-15) the result is **identical**: 620,630 whole journeys, 10,628,696 rows, 0 differing rows both ways, 29,504 journeys crossing local midnight (27,019 starting on 09-14 into the rewritten 09-15, 2,485 from 09-15 into 09-16), 9,601 closed night-line journeys of which 305 cross midnight, and 14 strict `N<digits>` lines that cross. That subset is **not** the date-level comparison and covers 10.6 million of the 13.6 million rewritten rows. Compared whole, 09-15 differs on 2,421,490 rows in 24,248 journeys.
- **Night N = 2026-09-17, chained on the state night 16 leaves (dbt 105 s, 26,967,836 read, 13,921,437 written):** local 09-16 is rewritten from data that stops at 23:57 UTC, which is 00:57 local on 09-17 in British Summer Time. **2,438,445 of 13,370,981 rows (18.2%, 24,314 journeys) have a wrong `journey_end_ts` and `journey_n_pings`** (61,472 also a wrong `last_moving_seq`, 2,335 a wrong `first_moving_seq`), all in journeys that end after the data cutoff. Both sides hold the same 13,370,981 keys and all other columns agree. The next night rewrites `>= max(this)`, which by then is 09-17, so 09-16 is never repaired.

This is the main open gap. It is a property of the window fix (commit e2f905c) interacting with British Summer Time; that it is present with the old key too is inferred from the shared window logic (the night simulations ran only the new model), and the old model never proved equality either. It is not caused by the operator key, but it means the incremental table is not equal to a cold build,. It was measured on a synthetic state built from the reference, not on a real chain of nightly builds. Not exercised at all: a journey that started before the read window and runs into the rewritten dates (the scratch data begins 2026-09-13, so none exists; in the cold table 2,668 journeys are longer than 24 hours and 88,444 longer than six). Such a journey would be re-keyed at the window edge.

The repair options, none built: rewrite `>= max(this) - interval '1 day'` (one more date written, size not measured; expected to fix the truncation, untested), or accept the truncation knowingly and document it. The choice is the owner's.

### The semantic effect on journeys, survivors and `eta_models`

Old model on the old extract against new model on the new extract, five days, in-sample, so model and extract change together:

| Measure | Old | New |
|---|---|---|
| Journeys (distinct `journey_key`) | 4,806,992 | 2,586,231 (-46.2%) |
| Survivors of `int_journeys_shaped_and_directed` | 1,228,561 | 1,371,776 (+11.7%) |
| Pings in surviving journeys | 33,285,965 (60.06%) | 37,065,807 (66.85%) |
| Training rows after trimming | 16,286,096 | 17,972,955 (+10.4%) |
| `eta_models` rows | 89,657 | 97,695 (+8,038) |
| Mean r2, whole table | 0.7711 | 0.7791 |
| Global fallback r2 | 0.4177 | 0.4368 |
| `line_punctuality` `n_journeys` | 530,281 | 662,163 (+24.9%) |

The old model shreds journeys where `bus_id` collides across operators (median pings per raw journey 3 against 10). The survivor share of journeys (25.6% to 53.0%) is an artefact of the halved denominator and should not be quoted; the absolute survivor count and the ping share are the fair figures. Survivor shape is essentially unchanged (median pings 14, median duration 2,340 s, path length median 8.994 km against 8.994, p90 20.108 against 20.325).

Verdict on the ETA fit: **the same, not worse.** On the 89,630 grains present in both, mean r2 moved by +0.00012 (median difference 0; 1,092 grains improved by more than 0.05 and 1,344 degraded by more than that). The whole-table +0.0080 is composition: 8,065 additional grains crossed the 200-observation floor. "Slightly better" is not supported. All r2 figures are in-sample with no held-out check. The eight assertions that were run on the new database passed (the full ~884-test suite, `int_pings_matched_to_stops` and the line_stops cluster were not run), including `assert_eta_model_*`, `assert_journey_shape_filters_held` and `assert_segmented_ping_seq_unique`. Marts will step-change on the deploy night: journeys, survivors, `n_journeys` and the `eta_models` size, and any panel or alert on them.

### Cold timings and memory

Cold segmentation of the five-day, 55.4 million row data through the wrapper (`memory_limit` 6 GB, three threads, 13,000 MB cgroup cap, one heavy model at a time): new model 811.3 s then 1,084.9 s wall, peak RSS 6,894 and 6,622 MB; old model 706.6 s then 1,068.9 s, 6,759 and 6,835 MB. **No speed claim is supported**: identical code varied by about 50% between runs, more than the gap between old and new. Peak memory sits just above `memory_limit` either way, so the sort key widening did not measurably change it. The shaped model took 18 s, and the downstream four-model rebuild about 40 s. The eight-day cold build (about 76 million rows) was **not run**: the extrapolation is 1.4 times the volume; on the production host the same model took 2,542 s in the 2026-09-20 nightly and 2,626 s in the cold verification, two to three times the scratch figure.

### What the verifiers found

- The night-A "identical" verdict is a restricted one (above), and the BST truncation is real wrong output on the 03:00 path.
- **The planned dry run consumes the migration on the wrong data.** `SKIP_EXTRACT=1` reuses the parquet on disk, written by the old `extract.py`: no `recorded_at`, cross-operator rows merged. D4 keys only on the shape of `journey_key`, so that dry run would rebuild everything with new-shape keys from old-shape files; the real 03:00 run would then see only new-shape keys, not force, and rewrite only two dates, leaving about six dates built from old extract data until they age out. Remedies: run the dry run without `SKIP_EXTRACT`, or follow it with a `FULL_REFRESH=1` on a fresh extract, or make D4 also force when the newest parquet lacks `recorded_at`.
- **D5 as prototyped is a nightly hazard.** `count(distinct struct)` took about 400 s and 6.1 GB, inside `dbt test`, before publishing. It must be written as group by key, then `having count(*) > 1`. The output-side version cannot include `recorded_at`, which is not carried into the table; the staging-side version can.
- **Untested downstream:** `int_pings_matched_to_stops`, `int_first_visit_per_journey`, the `int_line_stops_*` cluster, `int_stop_identity` and the full test suite did not run on the new data. Survivors and training pings rise about 10%, and `line_stops` has OOM'd before.
- **QuestDB memory.** The scratch extraction, without the nightly's restarts between days, reached 7.848 GiB in one poll (cap 8 GB) and 7.06 GiB on another day, then fell back; no OOM and no restart.
- **Extract fragility:** `operator` is now in `KEYS`, which the availability filter does not check, so a QuestDB without the column would give an error body that `read_csv` mis-parses (hypothetical, since the column has existed since 2026-09-12).
- **Rollback of a failed migration night:** `FULL_REFRESH` deletes the 10.5 GB warehouse before dbt starts, so a failure midway leaves a partial warehouse. The published marts are safe (publication is an atomic `mv` after tests and contract pass), and the next night does not re-force. The suggestion is to rename the old warehouse aside for one night instead of deleting it.

### What was not tested

The real systemd service, at all. The eight-day cold build. Concurrent production load (the ingestor, QuestDB and the API running as the build does). A real chain of nightly builds. `build.sh` with D4 in place. The rest of the pipeline listed above. The comparison of a journey longer than the read window.

### If it is applied later

It takes effect at the next 03:00 run after it is committed. The first run after deployment is a one-off `FULL_REFRESH=1` (D4), which deletes the warehouse and rebuilds everything: duration unknown (a guess of about an hour is unmeasured); the inputs are that cold segmentation alone took 811 to 1,085 s in scratch at five days and 2,542 to 2,626 s on the production host at the old key; **unproven** for eight days. Roll back by `git revert` of the commit; the previous good marts keep serving if the run fails, but because the old model reading a table with new-shape keys would mix shapes, the revert also needs one `FULL_REFRESH=1` run. Before applying: decide the BST window question, write D5 cheaply, and extend D4 (or the dry-run procedure) as above.

## Amendment: what was deployed

*Written the same night, after the fact. This is the current state and supersedes any "not applied" statement above.*

### What is in the repository

On `feature/TJP-20090`: `477ea0e` (deterministic segmentation, the extract change, the `build.sh` migration guard, the corrected window and the order test), `61543a2` (two source tests), `11674d0` (the API identity fix), `a22697d` (contracts and ontology), and `e2f905c` before them (the first, too-narrow window, which `477ea0e` corrects).

### What differs from the design proven in scratch

1. **The window is not the one the scratch proof used.** The proof, and commit `e2f905c`, read `>= max - 1 day` and rewrote `>= max`. The night simulations showed that leaves 18.2% of the previous local date's rows with truncated journey ends in British Summer Time, and the next run never repairs them. The deployed window reads `>= max - 2 days` and rewrites `>= max - 1 day`: about 27.5 million rows read and 16.4 million rewritten on the same night's numbers, against 40.9 and 27.5 million before 2026-09-20. **This correction was derived from the measured defect and was not re-simulated in scratch**, so "incremental equals cold" is still unproven for the deployed window. What was exercised is that it runs, passes all 887 tests and the contract gate in the real path (below).
2. **The order test is cheap and warns.** `assert_segmented_order_is_total` groups by `(operator, bus_id, ping_ts, line)` over the last two dates and has severity `warn`, instead of the prototype that took about 400 s and 6 GB and would have blocked publishing. It passed with zero rows.
3. **The migration guard was hardened** as the production verifier asked (visible message when the check is skipped, respects a preset `FULL_REFRESH`, safe under `set -e`) and re-tested on tiny DuckDB files: legacy keys, all-old keys, mixed, all-new, empty table, absent table, absent file, an invalid file, and a preset `FULL_REFRESH`.
4. **Two source tests had to change, and only failed on the first real run.** `expect_compound_columns_to_be_unique (timestamp, bus_id, line)` reported 37,231 duplicates because vehicles of different operators that the old extract merged are now separate rows, and `columns_match_set` did not know `recorded_at`. Both are correct consequences of the extract change and were updated in `61543a2`. Nothing in the scratch proof could have found them, because the scratch runs did not execute the source tests against a new-shape extract.

### The supervised migration run

Run by hand on 2026-09-20 with the real `build.sh`, under the production memory cap (`systemd-run --user --scope -p MemoryMax=13000M -p MemorySwapMax=0`) and the systemd `PATH`, with the nightly's container stop set down (osrm, backend, tiles, ollama, immich) and the ingestor, QuestDB and Grafana up. The 03:00 service itself was not used.

| Step | Measured |
|---|---|
| Real extract, 8 days (QuestDB restarted between days) | 9 min 40 s, 71,897,611 rows (2026-09-13 to 09-19; 09-12 had aged out of QuestDB) |
| Guard | "legacy journey_key shape detected: forcing FULL_REFRESH=1 (one-off migration)", warehouse wiped |
| Cold `int_pings_segmented_to_journeys` | 2,643 s (old model, cold, real service: 2,626 s) |
| Cold `int_pings_matched_to_stops` | 860 s, and it **passed at `memory_limit` 6GB** (the model that failed the 2026-09-20 03:00 run at the old 4GB limit) |
| Cold `eta_models` | 433 s |
| First dbt run, whole | 3,523 s (58 min 43 s), 11 of 11 passed |
| The 86 `line_stops` models | 224 s |
| Tests | 96 s; **884 passed, 2 failed** (the two source tests above), so nothing was published and the old marts kept serving |
| Whole first attempt | 1 h 14 min 15 s, largest process 11.5 GB resident |

After fixing the two tests the build was run again with `SKIP_EXTRACT=1` against the migrated warehouse, which did not force a rebuild:

| Step | Measured |
|---|---|
| **Incremental `int_pings_segmented_to_journeys`** | **60.8 s** (2,219 to 3,154 s in the four previous nightlies logged) |
| The 86 `line_stops` models | 231 s |
| Tests | 103 s, **887 passed, 0 warned, 0 failed** |
| Contract gate | 62 passed, 0 failed; ontology resolves 51 bindings |
| Whole run to publish | **9 min 52 s**, largest process 11.7 GB resident, exit 0 |

The largest-process figure is consistent with the repo's earlier note that resident memory runs about 4.7 GB above `memory_limit`; both runs finished under the 13000M cap, but the margin on the cold path is about 1.5 GB.

**Expected steady state, not yet observed:** the 03:00 nightly is the extract (9 min 40 s measured) plus this incremental build (9 min 52 s measured), so about 20 minutes, against about 66 before. That adds two measurements taken in separate runs and has not yet been seen as one service run.

### Effect on the published marts

`eta_models` 119,442 rows (was 102,143, +16.9%: more grains clear the 200-observation floor), mean r2 0.781 (0.773), median 0.854 (0.846); `line_stops` 3,233,014 (2,771,725); `line_localities` 65,443 (62,249); `line_punctuality` 21,981 (17,583, +25.0%); `localities` unchanged. Journeys fell 46% and survivors rose 11.7% in the scratch comparison, so anything that plots `n_journeys`, the model-fit size or the punctuality summary series in Grafana **steps once** at the migration; that is the change, not an incident. The r2 figures are in-sample and the extract and model changed together, so they show "no worse", not "better".

### What is not proven

- The real 03:00 service has not run this code. The hand run used the same script but not the unit's own hooks, container start/stop and nginx reload.
- Equivalence of the deployed window with a cold build (point 1 above).
- QuestDB's container reached 7.85 GiB of its 8 GiB in one poll during the scratch extraction with no restarts between days; the nightly restarts between days, and the hand run's extract did too and finished without incident, but that is one observation.
- The API image was **not** redeployed: there is no deploy automation (ADR-0004), so the identity fix in `backend/app.py` is in the repository and tested but the running container still has the old code and still hides about 3% of vehicles until it is rebuilt.

### Rollback

`git revert` of `477ea0e` and `61543a2` (and `e2f905c` if the old window is wanted), then one `FULL_REFRESH=1 ./build.sh`, because the old model must not read new-shape keys and has no detector. The published marts file was replaced in place by the publish step, so there is no backup of the previous marts; a rollback rebuilds them.

## Roadmap

Recorded in [docs/ROADMAP.md](../ROADMAP.md) as 1.14.0 (window fix and deterministic key deployed 2026-09-20, see "Amendment: what was deployed").
