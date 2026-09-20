# ADR 0009: A fast nightly build -- where the time goes, and the plan to cut it

**Status:** Proposed 2026-09-20. *Amended 2026-09-20 (see "Amendment: the window fix"): the window fix was designed and partly tested, but it is NOT applied; `int_pings_segmented_to_journeys.sql` is unchanged and no variant was proven.* Nothing in this ADR has been built into the model. It records a measured profile of the nightly build, three ideas that were investigated read-only and what they showed, and the plan (workflow "WF-S") for finding and proving the change that pays. No model, test or build script has been edited.
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

**Recommended, not built:** partition by `(coalesce(operator, '~none~'), bus_id)` and order by `ping_ts` plus enough columns (line, `journey_ref`, lat, lon) for a total order, and put the operator into `journey_key`. That is a semantic change: it changes the survivor set and possibly `eta_models`, so it needs its own measurement (journey counts, survivors, downstream fit) and a one-off full refresh, because existing `journey_key` values would have the old shape. `operator` has been 100% populated since 2026-09-13 and leaves the retention window entirely after the 2026-09-21 run, so the NULL-operator case is a transition detail only. It would also make every future equivalence test exact, which is the main reason to do it before further tuning of this model.

## Ceilings and what would change the plan

- If the harness finds the oracle **disagrees with the persisted table**, the existing incremental pipeline has been accumulating inconsistent history, and that becomes the first priority ahead of speed.
- If no variant is bit-identical and downstream-neutral, the fallback is the bounding-box decision (open in the ROADMAP): the segmentation model was recorded at 724 s over the old 22-day north-west window and is about 2,650 s now over a four-date window at all-GB scale (4.7x the vehicles). The windows differ, so that is a pointer to where the cost came from and not a like-for-like ratio.
- The fixed floor (extract, container stop and restart, contract gate, tests, publish) is measured by the stage profile; if it is large, it is the next target and not the model.
- `eta_models` varies from 6 s to 360 s for a reason nobody has established; if it turns out to be scheduling contention, `--threads` and model ordering are levers.

## Amendment: the window fix

*Written 2026-09-20, after a workflow ("stream A") tried to prove a narrower window. This section corrects the "not started" wording above. Nothing here has run in the real nightly.*

### Outcome, stated first

**The window fix is not applied.** The stream halted with no variant proven, and the repository was not edited: `dbt/models/intermediate/int_pings_segmented_to_journeys.sql` still reads `>= max(this) - interval '3 days'` and rewrites `>= max(this) - interval '2 days'`. The next 03:00 nightly therefore behaves exactly as before, reading about 40.9 million rows and rewriting about 27.5 million. Nothing in this section is in the working tree or in a commit, and nothing has been tested inside the real service.

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

## Roadmap

Recorded in [docs/ROADMAP.md](../ROADMAP.md) as 1.14.0 (planned; the window fix was tested in scratch but not applied, see the amendment above).
