# ADR 0008: Published timetables (BODS GTFS) replace inferred route shape; London stays inferred

**Status:** Accepted 2026-09-20. Phase 1 (delete `line_stops`/`line_localities`, adopt GTFS route shape) and Phase 2 (add a timetable-pinned ETA rung) are both scoped here. *Amended again 2026-09-20 (see "(h) The API rewire specification"): the alias policy is now an allowlist and the NCTR rule is built; the position-based tiebreak is specified from a measurement but not built.* *Amended 2026-09-20:* the Phase 1 GTFS build now exists and has been run against the real feed (see "Amendment 2026-09-20"), but nothing has been deleted and the API still reads `line_stops`; Phase 2 has not been started. `memtest86+` came back clean on 2026-09-20, so the build gate that previously held this is lifted -- see "Before any of this is built."
**Date:** 2026-09-20
**Related:** [ADR-0001](0001-line-stops-memory-and-batching.md), [ADR-0002](0002-line-stops-final-assembly.md), [docs/ROADMAP.md](../ROADMAP.md), `dbt/models/marts/line_stops.sql`, `dbt/models/marts/line_localities.sql`, `dbt/models/intermediate/int_pings_matched_to_stops.sql`, `dbt/build.sh`, `backend/app.py`

## Read this first

This ADR replaces the *route shape and stop sequence* half of the pipeline
with a published source and leaves the *ETA regression* half essentially
untouched. Those are two different joins against two different needs, and
they were measured separately this session because they have wildly
different coverage: route shape only needs to know which stops a
(operator, line) pattern visits, and that is 95.2% covered for every
operator except TfL. Per-vehicle ETA needs to know which specific scheduled
trip a specific vehicle is running right now, and that plateaus near 49% no
matter which join is tried. Conflating the two would have produced a wrong
decision -- either declaring victory on ETAs because route shape looks
solved, or rejecting the whole timetable on the grounds that "only half the
buses pin." Phase 1 is a replacement because route shape's join is strong
enough to retire the inference pipeline that produces it. Phase 2 is an
additional, optional rung in the existing prediction fallback chain because
the trip-pinning join is not strong enough to replace anything.

London is explicitly excluded from Phase 2's benefit and is not a gap this
ADR tries to close. TfL does not publish to BODS. That is accepted as a
standing regression for roughly a third of the live fleet, not solved here,
and recorded as a roadmap item (a TfL Unified API ingest) rather than
scheduled work.

## Context

### What the project does today, and what it costs

Route shape and stop order are currently *inferred*: the dbt pipeline
watches live vehicle positions, segments them into journeys
(`int_pings_segmented_to_journeys`), matches pings to the nearest NaPTAN
stop (`int_pings_matched_to_stops`), and assembles a per-line, per-stop
sequence out of repeated observations
(`int_stop_identity`, `int_first_visit_per_journey`, and 84 generated
`int_line_stops_*` batch/grain models feeding the `line_stops` and
`line_localities` marts). ADR-0001 and ADR-0002 are the record of what it
took to make that assembly step fit in memory at all: batching the grain,
splitting final assembly into stages, and the general shape of the current
84-model cluster is the residue of that fight.

The cost today, measured this session:

- 97 `.sql` files under `dbt/models/`, 5,244 lines of model SQL, of which 84
  files are the `int_line_stops_*` cluster that exists solely to build
  `line_stops` and `line_localities` without exhausting memory.
- 13 `.sql` files under `dbt/tests/`, 7 of which exist only to guard this
  cluster: `assert_ping_stop_match_within_radius`,
  `assert_stop_identity_names_are_single_valued`,
  `assert_line_stops_batch_coverage`, `assert_line_stops_grain_is_unique`,
  `assert_line_stops_has_day_type_fallback`,
  `assert_line_stops_has_operator_fallback`,
  `assert_line_stops_seq_is_dense`.
- A nightly build that takes 2.5-3 hours end to end (`docs/operations.md`).
- The 2026-09-20 03:00 nightly failed inside `int_pings_matched_to_stops`,
  which hit DuckDB's 4GB `memory_limit`. That model is not a peripheral
  detail here -- it is the busiest join in the inference pipeline
  (`stg_naptan__stops` matched against every ping) and it is a direct
  consumer of exactly the intermediate models this ADR proposes to delete.
  Its OOM is not an isolated bug to patch; it is this pipeline's memory
  ceiling being reached again, three months after ADR-0001 first hit the
  same wall for a different model in the same cluster.
- `dbt/build.sh` already carries a page-cache drop between its two `dbt run`
  invocations, with a comment recording that cgroup v2 counts page cache
  against `MemoryMax`. That workaround exists because of this cluster's
  memory behaviour and is itself evidence of how close to the edge the
  current pipeline runs.

And it does not work well even when it completes. `line_stops.sql`'s own
docstring records its measured route-order error: 0.46x on a coherent
single-operator loop (i.e. roughly right), and ~18,818x on a deliberately
worst-case reused line number (i.e. catastrophically wrong, because two
unrelated services sharing a line number get their stop visits folded
together by inference). The docstring already names the fix before this
ADR was written: "journey_ref/DirectionRef from the post-2026-09-12 feed,
operator in the mart grain, or simply reading the published timetable." This
ADR takes the third option, because the investigation below shows the first
two do not actually solve the harder problem (per-vehicle trip pinning) and
a published timetable solves the easier one (route shape) outright.

### The measurement that changed the plan

The starting hypothesis, going into this session, was that GTFS timetables
might let the project drop the fitted ETA regression (`eta_models`)
entirely and replace it with scheduled-time lookups. That hypothesis did
not survive contact with the live feed. What was measured instead is the
asymmetry that drives every decision in this ADR: route shape and
per-vehicle ETA need different joins, and those joins have very different
coverage.

## What was measured

### The GTFS file

Source: BODS GTFS, `https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/all/`
(public, no API key). Regional variants exist at
`.../gtfs-file/<region>/` -- `north_west`, `england` and `west_midlands`
were all confirmed HTTP 200, so a narrower pull is available if the bounding
box decision (open in `docs/ROADMAP.md`, "Open decision: the ingest
bounding box") ever moves back off all-GB.

The GB file published 2026-09-20 02:50:

- Zip: 1,341,351,819 bytes (1.34GB), downloaded in 117s.
- Uncompressed: 7.8GB across 10 files -- `stop_times.txt` 5.02GB,
  `shapes.txt` 2.57GB, `trips.txt` 194MB, `stops.txt` 20.9MB,
  `calendar_dates.txt` 3.8MB, `routes.txt` 327KB, `calendar.txt` 80KB,
  `agency.txt` 53KB, `frequencies.txt` 5.5KB, `feed_info.txt` 191B.
- `shapes.txt` is not needed: OSRM already produces route geometry for this
  project, and shapes.txt is 2.57GB of the 7.8GB uncompressed total --
  skipping it removes a third of the load for zero loss of anything the
  project actually uses it for.
- Loaded into DuckDB without `shapes.txt`: 990MB database file, 95.7s wall
  clock, threads=3, `memory_limit` 4GB. That is a load an order of magnitude
  cheaper, in both time and memory, than the nightly build's inference
  pipeline that it replaces.
- The North West region alone, same treatment: 102MB zip, 615MB
  uncompressed, 80MB DuckDB file, 6.3s load. Kept as a reference point for
  the still-open bounding-box question, not acted on here.

Row counts, GB-wide: 635 agencies, 13,717 routes, 1,528,546 trips, 311,902
stops, 2,112 calendar rows, 228,034 calendar_dates rows, 59,004,452
stop_times rows. Of the 1,528,546 trips, 230,695 have a service active on
2026-09-20 (a Sunday). Grouped to (agency_noc, route_short_name,
direction_id), there are 22,702 distinct route patterns.

`calendar.txt`'s `end_date` values run out to 20270620 -- the file is
forward-dated by BODS, which matters operationally (see below): a weekly
pull already contains the whole following week's planned services, so a
Monday refresh is not racing against Tuesday's timetable landing on
Wednesday.

### Key alignment: what joins cleanly and what does not

Three joins were verified against real rows and align directly, with no
crosswalk table needed:

- `stops.stop_id` *is* the ATCO code (e.g. `2500DCL792`, `1800STINAR1`),
  which is exactly what the live feed's `origin_ref`/`dest_ref` carry.
- `agency.agency_noc` *is* the NOC (e.g. `MHCO`, `BNSM`), which is exactly
  what the live feed's operator field carries. All 635 agency rows have a
  non-null NOC.
- `routes.route_short_name` *is* the line number as riders and the live
  feed both know it.

One join that was expected to work does not. `trips.trip_id` looks like
`VJ<sha1>`; `trips.vehicle_journey_code` looks like `vj_25`. The live
SIRI-VM feed's `journey_ref` carries values like `0910`, `22`, `998`,
`1910` -- a departure-time or sequence code, not a GTFS `trip_id` and not
the `vehicle_journey_code` either. There is no direct trip-ID join. A trip
has to be pinned indirectly, by combining operator, line, origin stop and
aimed departure time, and how well that indirect pin works is the whole
question Phase 2 turns on.

### The live feed, and who is in it

A 20-minute snapshot (latest ping per bus) across the whole GB bounding box
showed 22,587 distinct live buses. 21,192 of them (93.8%) carry enough
fields -- operator, line, origin aimed departure -- to attempt any join at
all. Over the preceding 3 hours (3,758,016 pings, 22,769 distinct buses),
field coverage was: operator 100%, origin_aimed_dep 93.1%, dest_ref 91.2%,
journey_ref 68.9%.

Operator TFLO (London) accounts for 7,365 live buses, 34.8% of the keyed
fleet, and has zero rows in the GB GTFS agency table -- TfL does not
publish timetables to BODS at all. Every other operator absent from GTFS is
negligible by comparison: MNWB 5, MINS 4, STAR 1, LAGC 1. Excluding TfL
leaves 13,827 keyed live buses that could, in principle, be pinned to a
GTFS trip.

### The join-variant table: why the exact-match rate plateaus

Seven join variants were tried against those 13,827 non-TfL buses. "Matched"
means at least one candidate GTFS trip; "unambiguous" means exactly one.

| Variant | Join | Matched | Unambiguous |
|---|---|---|---|
| A | noc + line + origin_stop + exact dep | 6,899 (49.9%) | 6,768 (48.9%) |
| B | noc + line + exact dep | 7,468 (54.0%) | 6,574 (47.5%) |
| C | line + origin_stop + exact dep (no noc) | 6,919 (50.0%) | 6,766 (48.9%) |
| D | line + origin + dest + exact dep | 6,780 (49.0%) | 6,647 (48.1%) |
| E | noc + line + origin + dest + exact dep | 6,766 (48.9%) | 6,646 (48.1%) |
| F | noc + line + origin, dep +/-15min | 8,815 (63.8%) | 7,521 (54.4%) |
| G | line + origin + dest, dep +/-15min | 8,388 (60.7%) | 7,198 (52.1%) |

Two readings drive the decision. First, dropping the operator from the key
(C versus A) changes almost nothing -- operator granularity is not the
bottleneck, so there is no cheap win from being more careful about NOC
matching. Second, widening the departure-time tolerance to +/-15 minutes
(F, G) buys matched coverage, but a meaningful share of that gain is
ambiguous: F matches 8,815 vehicles but only 7,521 of those unambiguously,
so 1,294 of them have more than one candidate trip and picking between them
is a guess. Set against A's 6,768, the wide window buys 753 additional
trustworthy pins at the cost of 1,294 untrustworthy ones. The exact-match, unambiguous rate plateaus near 49%
regardless of which fields are in the key. That plateau is the number Phase
2 is built around: not "widen the window until coverage looks better," but
"accept that roughly half of non-TfL vehicles pin cleanly and half do not,
and build a rung for the half that does."

### Why the other half fails: the bucket split

Splitting all 21,192 keyed live buses (TfL included, since this shows where
TfL sits relative to everyone else) by why they do or do not pin:

| Bucket | Count | Share |
|---|---|---|
| Exact match | 6,899 | 32.6% |
| Same origin, departure within 15 min | 1,916 | 9.0% |
| Same origin, departure far off | 1,911 | 9.0% |
| Line runs today, different origin stop | 810 | 3.8% |
| Line exists in GTFS but not active today | 1,886 | 8.9% |
| NOC present, that line absent | 394 | 1.9% |
| NOC absent from GTFS entirely | 7,376 | 34.8% |

The last row is, to within rounding, exactly TfL's 34.8% share of the keyed
fleet -- confirming that "NOC absent" is not a data-quality problem
scattered across many small operators, it is London.

A concrete example of the "departure within 15 min" bucket, because a
number alone does not explain the mechanism: a live bus operated by BNSM on
line 358 from stop `1000DHSRB893` advertises `origin_aimed_dep` 16:40:00.
GTFS has that operator, that line, and that origin stop departing at
:45 past every hour (09:45 through 18:45). The published timetable and the
operator's real-time system disagree by five minutes on when the same
scheduled departure happens. This is not a data-entry error in either
system; it is two systems that were never reconciled with each other, and
it is exactly the kind of drift that makes a wide time-tolerance join
dangerous rather than merely lenient -- widen the tolerance enough to catch
this case and it also starts catching genuinely different, unrelated
departures.

### Route-level coverage: the join that route shape actually needs

Route shape does not need a trip pin at all. It needs only to know, for a
given (operator, line), what stops that pattern visits and in what order --
a much weaker join, at (noc, line) grain rather than per-vehicle grain.

Measured over all live keyed buses: (noc, line) present in GTFS for 14,428
(63.9%); the line present but under a different NOC for 7,342 (32.5%,
almost entirely a TfL/route-numbering artefact); line absent entirely for
817 (3.6%). Excluding TfL, the picture is clean: (noc, line) present for
14,428, line absent for only 437, and only 299 present-under-another-NOC --
95.2% of non-TfL live buses have their (noc, line) published in GTFS.

That 95.2% against per-vehicle pinning's ~49% plateau is the decisive
asymmetry this ADR is named for. It is why Phase 1 (route shape) is
strong enough to retire an entire inference pipeline, and Phase 2
(per-vehicle ETA) is not strong enough to retire anything -- it can only
add a rung that some vehicles reach and most do not.

## Decision

### Phase 1: GTFS replaces inferred route shape

*Amended 2026-09-20:* the decision below stands, but its Phase 1 design was
refined by the first build. The GTFS tables keep every route pattern rather
than one per (operator, line, direction), and a learned alias table sits
beside them. The deletion described here has not happened; see the amendment
for the gate. The original text follows unchanged.

`line_stops` and `line_localities` are deleted and rebuilt from the BODS
GTFS database rather than from inferred pings. Verified by grepping every
`ref()` in the project, deleting those two marts cascades to, with no other
consumers surviving:

`int_stop_identity`, `int_first_visit_per_journey`,
`int_pings_matched_to_stops`, and all 84 `int_line_stops_*` batch/grain
models -- 87 models in the cascade, which together with the two marts
themselves accounts for 89 of the project's 97, plus the 7 tests named above
(`assert_ping_stop_match_within_radius`,
`assert_stop_identity_names_are_single_valued`,
`assert_line_stops_batch_coverage`, `assert_line_stops_grain_is_unique`,
`assert_line_stops_has_day_type_fallback`,
`assert_line_stops_has_operator_fallback`,
`assert_line_stops_seq_is_dense`).

Eight models survive with no changes needed to their own logic:
`stg_bods__vehicle_pings`, `stg_naptan__stops`,
`int_pings_segmented_to_journeys`, `int_journeys_shaped_and_directed`,
`int_pings_measured_to_journey_end`, `eta_models`, `localities`,
`line_punctuality`.

`int_pings_matched_to_stops` is, not coincidentally, exactly the model that
OOM'd the 2026-09-20 nightly build, and the `int_line_stops_*` cluster is
the entire subject of ADR-0001 and ADR-0002. Deleting this cluster removes
both the OOM source and the two years of accumulated batching machinery
that was built to work around it, in one move. It is not a coincidence that
the model that broke the nightly build and the models this decision retires
are the same models -- the cluster was already at the edge of what the
build could carry, on ADR-0001's own terms, before the GTFS file existed as
an alternative.

### Phase 2: a timetable-pinned ETA rung, additive only

`backend/app.py`'s `predict()` fallback chain today is: regression, then
`timetable_delay`, then `timetable_raw`, then `last_known`. Phase 2 adds a
new top rung, ahead of the regression, for vehicles whose trip pins
unambiguously under join variant A/C (noc/line dropped makes no
difference, so the simpler of the two is fine) -- roughly the 49% of
non-TfL vehicles established above. Everything that does not pin falls
through to the existing chain exactly as it does today. This is purely
additive: no existing rung is removed, weakened, or reordered below where
it already sits, and no vehicle's ETA gets worse because Phase 2 exists.
`next_stop`'s `aimed_iso` field, currently hard-coded `None` with a comment
that it stays null "until a timetable (GTFS/TransXChange) source is
ingested," is the field Phase 2 populates for pinned vehicles.

### London stays on the regression, deliberately

TfL's 7,365 live buses (34.8% of the keyed fleet) are kept on the existing
regression path. This is not a gap to be apologised for -- it is accepted
because TfL does not publish to BODS, there is no timetable to pin against,
and no join invented on top of GTFS changes that. The alternative (TfL's
own Unified API) is recorded as a roadmap item below, not built here.

### `eta_models` is kept

The fitted ETA regression continues to cover the ~51% of non-TfL vehicles
that do not pin to a trip, plus the entirety of London. Its measured fit
quality (mean r2 0.773, median r2 0.846, with 4,701 of 102,143 grains below
r2 0.3) and its in-sample MAE of 6-11 minutes -- wider than the 6-minute
on-time window the project already treats delay against via schedule
adherence rather than the regression -- are unchanged by this decision.
Whether `eta_models` still earns its computational cost once Phase 2 is
shipped and its actual marginal coverage can be measured is explicitly a
Phase 3 question. It is not decided here, and this ADR takes no position on
it beyond recording that the question exists and depends on data this
decision has not yet produced.

## Consequences

### What improves

- Route shape and stop order for 95.2% of non-TfL operators (by live
  vehicle count) comes from the operator's own published timetable instead
  of being reconstructed from noisy position pings, and stops being wrong
  by up to 18,818x on reused line numbers -- that failure mode is retired
  outright, because it was a property of inference, not of the data.
- The nightly build loses its largest and most fragile cluster *by model count
  and by memory behaviour*: 89 models (97 today, 8 left standing), 7 of the 13
  tests, and the specific model (`int_pings_matched_to_stops`) that just
  OOM'd production. **Amended 2026-09-20: this is a reliability gain, not a
  speed gain.** Measured from `dbt/build.log` for the last passing run
  (2026-09-19), the 86 `line_stops` models cost 200 s in total and
  `int_pings_matched_to_stops` 458 s, against a first `dbt run` of 3,131 s
  dominated by `int_pings_segmented_to_journeys` (2,649 s), which this ADR does
  not touch. Deleting the 89 models saves roughly 11 minutes of a nightly that
  takes about 66. See ADR-0009 for where the time actually goes. The memory-batching machinery from ADR-0001 and
  ADR-0002 becomes unnecessary for this purpose (it does not vanish from
  the codebase's history, but nothing downstream depends on it after this
  ADR).
- `aimed_iso` in `next_stop` becomes populated for pinned vehicles instead
  of permanently null, which is a real answer to the comment already in
  `backend/app.py` explaining why it was null.

### What gets worse or stays unsolved -- stated plainly, not softened

- TfL's ~34.8% of the live fleet has no published timetable at all and
  gets none of Phase 2's benefit. It stays on the regression exactly as
  today, with the regression's existing MAE of 6-11 minutes. This is a
  standing, accepted regression, not a temporary gap.
- Of the remaining non-TfL ~65.2% of the fleet, roughly half (join
  variants A-E all plateau near 48-49% unambiguous) will not pin to a trip
  under Phase 2 and also fall through to the regression, unchanged from
  today.
- A second source of truth for routes now exists. GTFS route shape and the
  project's own observed vehicle behaviour can disagree -- a published
  timetable describes what an operator intends to run, not what it
  actually ran on a given day (diversions, short-workings, and
  out-of-service moves are all invisible to GTFS). No reconciliation
  between the two is built in this decision.
- The published timetable can be stale relative to what the operator's own
  real-time system believes, as the BNSM 358 five-minute drift
  demonstrates concretely. Any consumer of `aimed_iso` needs to treat it as
  the operator's stated intent, not as ground truth for what will actually
  happen.

## Operational decisions

### Refresh cadence: weekly, Monday 01:00

The GTFS database is refreshed weekly rather than daily because
`calendar.txt`'s `end_date` values run out to 20270620 -- the file is
already forward-dated by BODS well past any single week, so a Monday pull
already contains the whole week's planned services and there is nothing a
daily pull would catch that a weekly one misses, short of a genuinely
unscheduled service change mid-week (which the operator's real-time feed,
not GTFS, is the correct source for regardless of refresh cadence).

### `shapes.txt` is skipped

The project's route geometry already comes from OSRM. `shapes.txt` is
2.57GB of the file's 7.8GB uncompressed size and contributes nothing this
pipeline uses -- skipping it is a third of the ingest cost removed for a
file the project was never going to read from.

### Storage location and the cgroup page-cache GOTCHA

The GTFS database lives on `/mnt/archive` (the SATA archive: 916G total,
844G free at measurement time; the root SSD has 218G total, 76G free and is
not where a growing archive belongs). `ghost-backend` must open it
read-only and query it -- it must **not** be loaded into Python the way the
existing marts are.

The reason is the same cgroup v2 behaviour `dbt/build.sh` already works
around between its two `dbt run` invocations: page cache is charged against
a cgroup's `MemoryMax`. `ghost-backend` currently loads every mart row into
Python (2.46GiB steady, 4.29GiB on a reload peak, inside a 6g container
limit; at a previous 1.5g limit it OOM-looped and ETAs were dead for
roughly 26 hours). A ~1GB GTFS DuckDB file read the same way would push
page cache for that read onto the same 6g ceiling on top of the existing
marts' footprint. Querying the file in place, rather than materialising it
into Python objects, keeps its cost bounded by what a single query touches
rather than by the file's full size sitting resident.

## Ceilings -- what would force revisiting this

- If the ~49% unambiguous trip-pin rate degrades materially (for example,
  if BODS operators' published-vs-realtime drift, illustrated here by BNSM
  358, turns out to be systemic rather than occasional), Phase 2's rung
  stops paying for the join complexity it adds and should be re-measured
  against `eta_models` alone before being kept.
- If TfL's data becomes available -- the TfL Unified API is recorded on
  the roadmap as a second, differently-shaped ingest -- London's exclusion
  from Phase 2 is no longer a structural limit and should be revisited as
  a new phase, not folded into this one.
- If GTFS route shape and observed vehicle behaviour are found to diverge
  materially on a specific operator or line (a diversion pattern that
  outlives a single service disruption, for instance), that operator or
  line is a candidate to fall back to the inferred approach this ADR
  retires -- which argues for not deleting the *knowledge* of how
  inference worked (this ADR, ADR-0001, ADR-0002) even though the code
  built from it is deleted.

## Before any of this is built

Phase 1 changes the nightly build, and the nightly build is exactly what
was running when the host hard-froze on 2026-09-19, with no OOM and no
kernel log entry, and again on 2026-09-14. RAM (32GB installed, 4x8GB,
three different stick models, non-ECC, rated 3200 running at 2400) is the
was the top suspect. `memtest86+` came back clean on 2026-09-20, which
rules the memory out and lifts the gate that previously blocked any change
to this build -- but it does not explain the freezes. Both remain
unexplained, with PSU and thermal now the leading untested candidates, and
neither is a reason to hold Phase 1. The practical consequence for this
work is narrower than it looks: Phase 1 makes the nightly build
substantially smaller, so if the freezes were load-related at all, this
change moves in the right direction. The first full `ops/verify_build.sh`
run after Phase 1 should still be watched rather than assumed.

(Separately, and not a repo decision: `page cache holds it after (31Gi
now)` in this session's own measurement refers to the kernel's reported
usable memory, not installed capacity -- 31Gi visible to the kernel out of
32GB physically installed is the normal small reservation for the kernel
itself and firmware-reserved regions, not evidence of a hardware fault by
itself.)

## Amendment 2026-09-20: what the first build measured

The sections above record the decision as it stood when it was written. This
section records what happened when Phase 1's GTFS build was first written and
run against the real feed, and what changed as a result. The original text
has not been rewritten; where a sentence above is now misleading it carries
an "Amended" marker pointing here. Every figure below comes from the
measurement reports of 2026-09-20 (the single-pattern comparison against
`line_stops`, the all-patterns comparison, and the live-pinning report) or
from the integration run's own log.

### (a) Single-pattern Phase 1 covered 80.6% of trips, and hid number reuse

The first design kept one "primary" pattern per (operator, line, direction).
Measured: 51,446 distinct patterns over 1,528,546 trips, and the primaries
covered 80.6% of trips. One trip in five ran a pattern that the served table
did not contain. Worse, the loss was not random. Operators that reuse a line
number for unrelated services in different towns collapse to whichever
variant is most frequent: SCEM line 1 has 42 live buses spread over 16
distinct patterns, of which only 19% are on the primary, so a primary-only
table would give roughly 81% of them the wrong town's stop list. SYRK 1 has
19 live buses on 4 distinct patterns, 3 of them on a circular pattern. This
is a finding that changes the Phase 1 design, not a tuning detail: the ADR
promised to retire the reused-line-number failure of inference, and a single
pattern per key would merely have reproduced it in smaller form.

### (b) Decision: keep every pattern, with a pruning rule

The owner decided to keep every pattern rather than one. A pattern is kept
if it is the primary for its key or has `n_trips >= 2`; single-trip
one-offs that are not the primary are pruned to bound the table. Each
pattern carries `pattern_id`, `first_atco`, `last_atco` and `is_circular`.
The integrated run kept 42,009 of 51,446 patterns (22,702 primary and 19,307
non-primary); the kept patterns cover 99.4% of trips, against 80.6% for the
primaries alone. `gtfs_route_stops` grew to 1,499,050 rows (1.88x the 798,737
of the primary-only build, whose primary rows are unchanged) and
`gtfs_route_patterns` holds 42,009 rows. The pruning has a cost that is
unproven rather than measured to be small: the live-pinning report found 119
unpinned buses whose refs match a raw trip but no kept pattern, and could not
tell one-offs from trips on expired services, so the pruning rule's true cost
is not established.

### (c) A learned alias table, and why a static one was rejected

Live operators and GTFS do not always agree on a line's label. The new
`gtfs_line_alias` table maps a live (operator, line) to the GTFS (operator,
line) that it corresponds to. It is learned, not written by hand: from 24
hours of live pings, the `origin_ref`/`dest_ref` pair of each (operator,
line) is matched to the first and last stops of GTFS patterns. A mapping is
kept only if at least 3 buses support it (`n_buses >= 3`) and one target
takes at least 60% of them (`share >= 0.6`); ambiguous mappings are dropped.
If QuestDB is unreachable at build time, the previous database's alias rows
are carried over rather than losing the table. The integrated run learned
631 rows. Carry-over was exercised separately with QuestDB pointed at a dead
port: it returned the 631 rows with the previous database, and returned 0
with an empty table when there was no previous database; neither raised. The
failure-leaves-the-old-file-intact promise of the refresh script was not
exercised, because no run failed.

A static rule table was rejected because the mapping cannot be derived from
GTFS alone. trentbarton's brand labels (`if`, `swi`, `vil`) appear on the live
side and nothing in the timetable says that `if` is GTFS `IF`, `swi` is `SW`
and `vil` is `V1`; only the observed endpoints reveal it. The learner did
recover all three, plus case and naming variants (KBUS `sky`, TBTN `one`).
It also has limits. It cannot learn NCTR's `NT1`/`NT7`/`NT88` because NCTR
pings carry no `origin_ref` and use non-NaPTAN destination codes; a
strip-`NT` rule would cover 159 of 161 NCTR buses and 59 of 62 NCTR keys, and
was not built when this paragraph was written (*amended: it is now built as a rule, see (h)(iii), and the coverage figure there differs*). And its quality is mixed: 604 of the 631 rows (96%) are TfL,
and several non-TfL rows look wrong (HUYT `2481`>`248`, median 2.5 km off;
MAGH `101`>`Beatles Explorer`; TBTN `ra`>`RA`, maximum 3.9 km; BRTB `sky`>`SN`
and `cot`>`COT`; UNIB `341`>`641`; BNSM `181`>`182`; WHIP `U2`>`U1`). The
aliases were learned from a window that contains the same buses they were
then tested on, so their pin rate is optimistic and not a held-out result.

The TfL rows deserve a separate warning. Every one of the top 25 aliases by
support is TFLO and looks wrong by label (TFLO 158 to ARVA 38). They are
nevertheless geographically consistent: 76.8% of aliased TFLO buses lie
within 120 m of a stop of the aliased route, against 78.2% for buses with a
direct match. Either the live TFLO `line` field is not the public route
number, or the refs are; the data cannot say which. Since London stays on the
regression by decision, the API must not apply TFLO aliases without a
further decision.

*Correction, later the same day.* The cause was a rule in this ADR's own
alias design: "if the live operator has no same-operator pattern anywhere,
allow any operator". That is unsound exactly when the live operator does not
exist in GTFS at all, because two unrelated routes that happen to share
termini then get relabelled as each other. It is now restricted: a
cross-operator match is allowed only if the live operator is an
`agency_noc` in `gtfs_agency`. Re-run against a copy of the served database
and the live QuestDB, the learner yields 27 aliases and none for TFLO (TBTN 10,
BRTB 6, KBUS 2, HUYT 2, and singles); most are case or brand fixes
(`one`>`1`, `61`>`6.1`, `ra`>`RA`, `igo`>`IGO`). The figures in the two
paragraphs above (631 rows, 604 TfL) describe the first build; the served
database from that build still holds them until its next refresh, and nothing
reads the table yet. The remaining non-TfL rows that looked wrong (`sky`>`SN`
at 78% share, `181`>`182`, `2481`>`248`) are same-operator near-label matches
that a shared terminus supports and that only a held-out check can judge. *(Amended: those 27 are now proposals, not applied aliases. Each was reviewed by hand and the accepted ones alone are applied; see (h)(ii). `sky`>`SN`, `181`>`182` and `2481`>`248` were rejected.)* (This measurement also shows that GTFS does hold London
operators' routes, such as ARVA, ELBG and GAHL. The statement above that TfL
does not publish to BODS is about the TFLO operator, which has no `agency_noc` = TFLO row in
`gtfs_agency` (queried: 0 rows), and remains true of it.)

### (d) Clockwise and anticlockwise are never labelled in GTFS

GTFS has `direction_id` 0 and 1, never "clockwise" or "anticlockwise". Live
circular services report those words in the direction field, so a direction
match against GTFS cannot work for them. Circular patterns are marked
`is_circular`, and the API must resolve a circular bus by pattern match on
its refs, ignoring the direction label. Measured on 172 pinnable live
clockwise/anticlockwise buses, only 36% (62) pin to an `is_circular`
pattern, 41% pin to non-circular patterns (loops with distinct first and
last stops) and 23% are unpinned, so "circular-only" would be the wrong rule.

### (e) Measured effect, including what did not improve

Live vehicles (22,384 buses; 7,393 TFLO excluded, leaving 14,991): 14,249
match directly, 166 through an alias and 576 not at all, so alias lifted
coverage from 95.05% to 96.16%. Of 14,405 pinnable buses, matching first and
last stop against every pattern gives 54.8% pinned to the primary, 20.1% to
a non-primary, 18.4% to several patterns and 6.6% unpinned. A primary-only
table would be wrong for 23.5% of pinnable buses (2,902 on a non-primary
pattern and 486 ambiguous without the primary among the candidates), and
between 23.5% and 38.6% once the 15.1% of ambiguous cases that do include
the primary are counted as possibly wrong. Keeping every pattern recovers
the 23.5%. It does not resolve the ambiguity: 2,473 of the 2,656 multiple-match
buses match variants in the same direction with identical endpoints, and
first and last stops alone cannot separate them. The 952 unpinned buses are
dominated by National Express coach-station codes (338) and short-turn or
part-route working; the existing 120 m nearest-stop fallback still gives 524
(55.0%) of them a stop.

Against `line_stops` (5,780 real-operator non-TfL keys): key coverage is
94.2% direct (5,445) and 94.6% with the alias (5,469). Direction ignored,
the Jaccard overlap of stop sets against the primary has median 0.429 and
57.4% of keys below 0.5; against the union of all patterns the median is
0.452 with 54.9% below 0.5. Containment of the `line_stops` stops in the union
has median 0.806 against 0.778 for the primary; pooled, 74.9% of
`line_stops` stops lie in the union against 66.8% in the primary, and 85,388
stops (25.1%) are in no GTFS pattern for their line. Locality pairs: 78.0%
of the old pairs are found in the new table (71.6% before).

What did not improve, stated plainly. Order agreement is unchanged: the
best-pattern Spearman median is 0.975 (0.976 before) and 10.2% of keys are
below 0.5 (10.3% before); the best pattern rescues only 51 of 695
low-correlation keys, because the cause is `line_stops` merging reused line
numbers, not missing GTFS patterns. GTFS gives the shorter path in 92.6% of
disagreeing keys, unchanged. Direction-aware Jaccard against the primary is
identical (median 0.325, 8,090 of 8,877 keys below 0.5) and is slightly worse
against the union (8,120 below 0.5). Key coverage moved only from 94.2% to
94.6%; 311 pairs stay unresolved, 142 of which exist in GTFS under a
different operator. NCTR is still 62 of 62 missing. The 20 busiest keys stay
middling (median Jaccard 0.64 against the union). Direction labels still
disagree: 201 `line_stops` direction keys have no matching GTFS direction and
737 of 8,919 keys match a pattern in the opposite direction better. Lookup
latency did not improve: route lookup p50 2.79 ms, p95 4.22 ms (2.64 ms and
3.61 ms with one pattern), warm cache. The disk cost of `gtfs_route_stops`
rose from about 26 MB to about 43 MB.

The integrated build itself: 246 s wall clock, peak process-tree RSS of 4,779
MB, lowest host available memory 13,865 MB, 1,054 MB database file.

### (f) API work that remains before `line_stops` can be deleted

`backend/app.py` still reads `line_stops`, and the GTFS tables do not yet
offer everything it reads. The gaps recorded by the first comparison are:
`day_type`; the pooled `*` fallback rows; `n_obs`; the scheduled
`median_secs_from_origin`, which is not the observed one (the median ratio of
observed to scheduled is 0.85); and a global lookup by ATCO code. Beyond that
gap list, the live-pinning work adds three items: choosing between same-endpoint
variants needs a second signal such as position along the pattern (specified
in (h)(i), not built); circular buses must be resolved by pattern match; and the alias table
must not be applied to TFLO without a decision (*amended: resolved by the reviewed allowlist in (h)(ii), which contains no TFLO row*). Nothing else on this list has been
done. Deleting `line_stops`, `line_localities` and the 87 dependent models
waits on all of it.

### (g) Does the coverage argument stand?

Yes, with a narrower claim than the original wording. The argument was an
asymmetry: route shape needs only (operator, line) and is strongly covered,
while per-vehicle trip pinning is weak. The new numbers support the first
half: 95.05% of non-TFLO live buses match a GTFS key directly and 96.16% with
aliases, in line with the original 95.2%, and 94.6% of `line_stops` keys
resolve. They do not disturb the second half, which was not re-measured
(trip pinning by aimed departure is unproven in this build; what was measured
is pattern pinning by endpoints). What changes is the strength of "route
shape is solved". Having the key is not the same as having the right stop
list: only 54.8% of pinnable buses pin uniquely to the primary, 18.4% are
ambiguous, and the stop-set agreement with `line_stops` is low (median
Jaccard 0.452 against the union). Those disagreements are consistent with
`line_stops` being the wrong side (the original ADR's own reused-line-number
evidence, and the 92.6% shorter-path result), but that is an inference, not a
proof: no ground truth for the real stop list of a line was used.

### (h) The API rewire specification

*Added 2026-09-20, extending (f). (It is lettered (h) because (f) and (g) already exist above; it is the specification of the pinning and alias behaviour the rewire needs.) Everything here is a specification or a measurement. None of it is in `backend/app.py`, which still reads `line_stops`, and the refresh that would publish the reviewed alias table has not been run since the review (see (ii)).*

#### (i) The position-based tiebreak

**Why endpoints alone cannot select a pattern.** Pinning a live bus to a GTFS pattern by its `(operator, line, origin_ref, dest_ref)` leaves several candidates for 18.4% of pinnable buses (see (g)). Almost all of that is same-direction variants that share both termini: 2,473 of 2,656 ambiguous cases. They differ in the middle of the route (a diversion, a school-day variant, a detour through an estate), so a second signal is needed, and the obvious one is where the bus actually is.

**What was measured.** Truth was built from the ambiguous buses that carry an aimed departure and match exactly one trip on `(operator, line, origin stop, aimed departure)`: **1,257 truth buses out of 2,636 ambiguous** at one snapshot (a Sunday, about 14:25 UTC). Candidate algorithms were scored against it (A1 to A5 use the last five pings against the stops that separate the candidates; A4 is the no-position prior, the highest `trip_share` pattern):

| Algorithm | Accuracy | Buses resolved without a tie |
|---|---|---|
| A1 | 72.6% | 7.1% |
| A2 | 72.5% | 7.1% |
| A3 | 72.2% | 7.2% |
| A4, the prior | 72.4% | 93.7% |
| A5 | 72.5% | 7.3% |
| A6, mean distance to each candidate's stop-to-stop polyline, last five pings | 71.4% | 12.0% |
| A7, the same distance accumulated over the whole journey trail (up to 90 minutes, 250 m cap per ping) | 74.7% | 29.5% |
| A7 used only when it leads by 3 m or more, else the prior ("hybrid") | 77.8% | |

With about 1,257 truth buses the sampling error is roughly plus or minus 2.5 points, so **last-five-ping position does not beat the prior**: A1, A2, A3 and A5 sit inside the noise of A4. The reason is geometric. Variants share most of their stops (a median of 5 separating stops out of 52), so only 153 of the 1,257 truth buses had a recent ping within 120 m of a separating stop. This contradicts the assumption in (e) and in `gtfs/patterns.sql` that position along the pattern is a strong second signal over a short window. Accumulated evidence helps modestly.

**Recommended algorithm: the hybrid (A7 with prior fallback), 77.8%.** Keep a running mean distance from each ping to each candidate pattern's stop-to-stop polyline over the current journey. If the best candidate leads the runner-up by **3 m or more**, use it; otherwise use the pattern with the highest `trip_share` (ties: lowest `pattern_id`). Above the 3 m margin A7 is 91.0% accurate on 19.7% of ambiguous buses (391 truth buses). Below 1 m it is no better than the prior (68.2% against 71.3% among the 781 truth buses with a gap under 1 m). In the hybrid, 102 buses were right only because of the trail and 34 were right only under the prior. A7 needs about 15 or more pings of trail (15 to 20 minutes) to pay: 71.0% with one to five pings and 70.5% with six to fifteen, against 75-76% with sixteen or more. The trail state must reset when the journey changes and is lost on an API restart (re-seeding from QuestDB took 0.37 s for 223,000 rows).

**Unresolved policy.** When no candidate clears the margin, fall back to the prior's pattern and its next stop, and **flag the result as unresolved** so callers can tell. Refusing to answer was rejected: the prior's stop set has a mean Jaccard of 0.961 with the true pattern even when the pick is wrong, all candidates agree on the next stop for 90% of truth buses, and every algorithm gets the next stop right 97.9% to 98.5% of the time. Returning the union of candidates was rejected: its precision is 0.928.

**Per-poll cost.** Ambiguous buses are 17.8% of live non-TfL buses (2,630 of 14,770), so about 1,070 per 6,000-vehicle poll (scaled, not measured on a real poll). Candidate patterns and stop coordinates should be cached per `(operator, line, origin_ref, dest_ref)` until the GTFS database is swapped, because `pattern_id` is not stable across refreshes: 1,272 keys, 2,866 patterns and 146,652 stop rows network-wide, fetched in one batched query in 0.29 s warm (3.7 s cold). Scoring one new ping against all candidates in pure Python took 0.32 s for 2,630 buses (0.12 ms a bus), about 0.13 s a poll, which merits a worker thread. Re-reading the trail on every poll would be about thirty times the work and is not recommended.

**Weak spots, stated.** (1) The truth set covers only buses with an aimed departure today that match exactly one trip, and the served calendar starts today; per-operator accuracy was not measured. (2) There are no truth buses with six or more candidates (14 ambiguous buses have that many), so accuracy there is unmeasured, and the 3 to 5 candidate bucket has about plus or minus 8 points. (3) The 3 m margin and its 91.0% were tuned and reported on the same snapshot, so they are optimistic; a second snapshot must confirm them before the threshold is fixed. (4) With no shapes (they are skipped), the polyline is straight stop-to-stop lines, which adds tens of metres of error; a mean distance above about 60 m should be treated as uninformative. (5) A GPS-jitter filter of the type in A3 removed the true pattern for 60 of 1,257 truth buses. (6) The cost figures are scaled from one network-wide snapshot.

**Side findings that correct earlier statements.** The direction mapping flagged as unmeasured in `gtfs/patterns.sql` holds: live outbound/inbound equals the GTFS direction for 10,014 of 10,022 single-candidate non-circular buses (99.92%) and for all 1,244 truth buses that carry it. In this sample clockwise mapped to GTFS outbound for 28 of 28 single-candidate non-circular buses and anticlockwise to inbound for 30 of 30; that is a small sample and does not overturn (d), and a direction filter adds nothing because candidates span more than one direction in only 8 of 2,630 ambiguous buses. The live `direction` field also carries `1`, `2` and `southbound` (46,261, 49,472 and 26 rows in fifteen minutes), so a direction filter must map only outbound and inbound and ignore the rest. The 18.4% in (g) is over 14,405 pinnable buses; this run's 2,630 of 13,265 buses with any candidate (19.8%) is over a different denominator.

#### (ii) The alias policy

**Learned aliases are proposals; only reviewed ones are applied.** `gtfs/build_alias.py` still learns candidate aliases from 24 hours of live pings, but a learned row reaches `gtfs_line_alias` only if `gtfs/alias_review.csv` holds an `accept` row for that exact `(live_operator, live_line, gtfs_operator, gtfs_line)`. A `reject` row for a pair beats an `accept`. A pair not in the file is dropped and logged as a WARNING proposal. The policy **fails closed**: a missing, unreadable or malformed CSV applies no learned aliases and logs a warning, and the rule rows (iii) are still built. When QuestDB is unreachable the carried-over previous rows are filtered by the same allowlist, and the fallback in `gtfs/fetch.py` carries only rows that are learned, still accepted, and whose target still exists in the new feed. Rule rows are not carried by the fallback, which is deliberate: they are regenerated by the next successful build.

**Why an allowlist.** The first build served 631 aliases, 604 of them TfL, wrong by construction (see (c)). A confidence threshold (`n_buses >= 3`, `share >= 0.6`) cannot tell a case fix from two unrelated routes that share termini, and after the cross-operator fix the 27 remaining rows still contained near-label matches that look plausible and are wrong (`181` for `182`, `2481` for `248`). A human decision, recorded with a reason, is the only check that catches that class, and it costs a minute a row on a table that changes rarely.

**The rubric.** Accept a same-operator pair whose label difference is only case, punctuation, a brand or code name, or a number written as a word, and whose live buses lie within 120 m of the target's stops for at least 80% of buses. Reject a cross-operator pair, any pair whose label differs by a digit or a letter, any pair whose share is below 0.9, and any pair whose geography contradicts it; a case-only pair is exempt from the 80% bar unless under 50% of its buses are within 120 m. The 50% contradiction bar is the reviewer's own reading of the rubric, not an earlier rule.

**The 27 reviewed.** The learner produced 27 proposals. As the CSV now stands **14 are accepted and 13 rejected**. (The first review, and the real run below, had 15 accepted and 12 rejected; a later re-check of TBTN `swi`>`SW` found 3 of 5 buses within 120 m over 24 hours, below the bar for a pair that is not case-only, and flipped it to reject.) Accepted: BRTB `cot`, `igo`, `key`, `mln`; KBUS `sky`, `sp`; TBTN `61`, `64`, `cmt`, `if`, `mic`, `one`, `two`, `vil`. Rejected, by reason: cross-operator (BORD `B1`>PERY, FCYM `4`>NADT `4A`, WNGS `HHL`>NXHH); a digit or letter differs (BNSM `181`>`182`, HUYT `1591`>`159`, HUYT `2481`>`248`, WHIP `U2`>`U1`); geography contradicts or falls under the bar (BRTB `cal`>`CC` 4 of 6, TBTN `60`>`6.0` 3 of 4, TBTN `ra`>`RA` 4 of 9 with a median 150 m, MAGH `101` to a name, 1 of 3, TBTN `swi`>`SW`); share under 0.9 (BRTB `sky`>`SN` at 0.82). The CSV holds each reason. No TfL row is present.

**Caveats.** Geography used the last 30 minutes where at least three buses were live and a 24-hour window otherwise (six pairs), so the windows are inconsistent and several samples are three to five buses (67% is 2 of 3). A verifier's 24-hour re-check gave different figures for two pairs (`swi`, now rejected, and `cal`, whose 0.83 is above the bar but which stays rejected, the safe direction). BRTB `cot` was accepted on 2 of 3 because it is case-only and the sample is too small to contradict it.

**State.** On the real served-database copy the alias table built to 109 rows: 15 learned and 94 rule. With `swi` now rejected the expected figure is 14 learned and 94 rule, 108 rows; that has not been run. The refresh (`gtfs/fetch.py`) was **not** run end to end after these changes and `verify()`'s new learned-row check compiled but has not been executed, so the served database still holds the first build's 631 rows until the next refresh. The failure fallback was exercised on scratch tables (old six-column schema, new schema, no previous database, dead QuestDB) and behaved as described. Nothing reads the table yet.

#### (iii) The NCTR rule

**What it is.** A rule in `build_alias.py` (`PREFIX_RULES = {"NCTR": "NT"}`) that, for every NCTR line in `gtfs_route_patterns`, emits an alias from the live label `NT` + line to the GTFS line, marked `source = 'rule'`. It never shadows a real match: no row is emitted if GTFS itself publishes the prefixed label or the alias already exists, and GTFS holds no NCTR line starting `NT`.

**Why a rule and not learned.** NCTR pings carry no `origin_ref` and use non-NaPTAN destination codes, so the learner has nothing to match; the mapping is a pure label convention (the feed's `NT1` is GTFS `1`), which GTFS alone supports and only a rule can capture. Rule rows need no review because they cannot contradict the timetable.

**Measured coverage.** 94 rule rows. Of 215 live NCTR buses (last 30 minutes, latest position per bus) all carry an `NT` label and 211 resolve through the rule, **98.1%**. The four that do not are buses 251, 282 and 288 on `NT18` and bus 252 on `NT18A`; GTFS has no NCTR line 18 or 18A, so no row can exist. The earlier estimate was 159 of 161 buses (98.8%); the fleet in the sample is larger now (215), so the two are different snapshots and not a like-for-like change.

**Ceiling.** One operator and one prefix are hard-coded. A second operator with a systematic prefix needs a line in `PREFIX_RULES`. It is deliberately not generalised: guessing prefixes for other operators is exactly the unreviewed, wrong-by-construction behaviour the allowlist exists to stop.

## Roadmap

The TfL Unified API is recorded in `docs/ROADMAP.md` as a future,
unscheduled item: a second ingest with its own schema, separate from BODS
GTFS, that could in principle close the London gap this ADR leaves open.
It is raised and plausible, not scheduled and not decided, pending its own
investigation into what that API actually exposes and at what cost.
