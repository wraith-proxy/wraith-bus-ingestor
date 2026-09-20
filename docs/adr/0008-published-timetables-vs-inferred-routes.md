# ADR 0008: Published timetables (BODS GTFS) replace inferred route shape; London stays inferred

**Status:** Accepted 2026-09-20. Phase 1 (delete `line_stops`/`line_localities`, adopt GTFS route shape) and Phase 2 (add a timetable-pinned ETA rung) are both scoped here; neither has been executed yet. `memtest86+` came back clean on 2026-09-20, so the build gate that previously held this is lifted -- see "Before any of this is built."
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
- The nightly build loses its largest, most fragile cluster: 89 models (97
  today, 8 left standing), 7 of the 13 tests, and the specific model (`int_pings_matched_to_stops`) that just
  OOM'd production. The memory-batching machinery from ADR-0001 and
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

## Roadmap

The TfL Unified API is recorded in `docs/ROADMAP.md` as a future,
unscheduled item: a second ingest with its own schema, separate from BODS
GTFS, that could in principle close the London gap this ADR leaves open.
It is raised and plausible, not scheduled and not decided, pending its own
investigation into what that API actually exposes and at what cost.
