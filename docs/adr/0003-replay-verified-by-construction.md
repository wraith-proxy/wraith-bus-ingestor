# ADR 0003: `replay` is verified by construction, not by a full rehearsal

**Status:** Accepted
**Date:** 2026-09-15
**Related:** [docs/ROADMAP.md § 1.3.0](../ROADMAP.md), [ADR-0001](0001-line-stops-memory-and-batching.md), `ops/incident.py`, `dbt/build.sh`

## Context

`ops/incident.py replay <start> <end>` landed in 1.3.0 as the incident
recovery path: restore archived days from `/mnt/<archive>`, re-extract them,
and rebuild the marts. It shipped untested, and 1.3.0 closed with an open
bullet saying so — "needs a synthetic 'lost N days' scenario run end to
end before it's trusted in a real incident."

Planning 1.4.0 turned up a defect in it before any rehearsal was run.
Both incremental models' retention post-hooks delete rows older than
`max(date) - retention_days()`, and `replay` only restores days older
than `today - RETENTION_DAYS`. Those are the same boundary. Every day old
enough to need restoring from cold storage was old enough to be pruned
again in the same run, so `replay` recovered the partition into QuestDB,
wrote the parquet, loaded it into dbt — and then deleted it. It silently
no-op'd for the only case it exists to handle.

## Decision

Fix the defect, verify the fix at the layer where it is decidable, and
**do not run the full end-to-end rehearsal.** The 1.3.0 bullet is closed
by this ADR rather than by a rehearsal.

## Rationale

The rehearsal is expensive and its own hazard. `replay` invokes
`FULL_REFRESH=1 build.sh`, a from-scratch rebuild of every incremental
model including the `line_stops` cluster — 20–30 minutes, and the model
with a documented history of taking the whole 15.4GB host down rather
than failing gracefully (ADR-0001; a host OOM killed a working session on
2026-09-14). Running it means stopping `ghost-osrm`, `ghost-backend` and
`ghost-tiles`, taking the public map and API offline for the duration,
and wrapping dbt in an OS-level memory cap because `profiles.yml`'s
`memory_limit` is DuckDB's own accounting and not a ceiling the kernel
enforces.

Set against that, what the rehearsal would actually tell us is small,
because the risky part was already decided elsewhere:

- **The defect is confirmed, not suspected.** Queried directly against
  the live build warehouse: both `int_pings_*` tables hold a 9-day
  window, and the post-hook's own predicate evaluated against a replayed
  `2026-07-03` returns true on both. That is the bug, measured.
- **The fix is confirmed at the layer that decides it.** The hooks are
  now wrapped in `[] if var('replay', false) else [...]`. Parsing the
  project both ways shows the normal run still carries both `delete`
  hooks and `--vars '{replay: true}'` carries none. Whether the hook runs
  is settled entirely at parse time — a rebuild cannot answer it more
  authoritatively than the manifest does.
- **The rest of the path is already proven.** `restore()` was exercised
  against a real archived day in 1.3.0 (2026-07-03, 10.6M rows, re-run
  correctly no-ops). `extract_day()` reuses `extract.py`'s own
  `build_select`/`fetch` rather than reimplementing them, so it is the
  same code the nightly run exercises daily. `build.sh` under
  `SKIP_EXTRACT=1 FULL_REFRESH=1` is an existing supported mode, not a
  new one.

What a rehearsal would genuinely add is integration risk between those
proven pieces. That is real but modest, and it is not worth a public
outage plus a known OOM hazard to buy it speculatively.

## Consequences

**Accepted risk.** The first real `replay` will be the first full
execution of the whole path. If it fails, it fails during an incident,
which is the worst time. This is a deliberate trade, not an oversight.

Mitigations that make it a reasonable one:

- `restore` is idempotent and separately proven, so the recovery can be
  driven a day at a time by hand if `replay` misbehaves.
- `bus_positions` has `DEDUP ENABLE UPSERT KEYS(bus_id, timestamp)`, so
  re-running a partially-completed replay does not double-count.
- `build.sh` publishes only after tests pass, so a bad replay leaves the
  previous good marts serving rather than replacing them.
- QuestDB is the system of record for recovery; the marts are derived and
  can always be rebuilt again.

**Not a roadmap item.** This deliberately does not become a "test replay
properly" entry. A rehearsal is worth running the next time a full
`FULL_REFRESH` rebuild is happening anyway for another reason — fold it
into that window, where the cost is already being paid. Carrying it as
standing work would misrepresent a considered trade as an outstanding
task.

**Revisit if** the retention window and the archive boundary stop
coinciding, `replay` grows beyond restore + extract + build, or a real
incident exercises it and finds something. Any of those changes the
calculation above.
