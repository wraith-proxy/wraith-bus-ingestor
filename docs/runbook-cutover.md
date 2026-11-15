# Runbook: contract-first dbt cutover

One weekend run of `ops/cutover.sh` (spec #43, card #56). It switches the nightly
build from the hand-written dbt project to the contract-first one without
losing history. The 8-day history (`int_eta_daily_stats`, `int_journey_delays`,
segmentation) exists only inside the persistent
`dbt/wraith_bus_ingestor_build.duckdb`; `dbt/data/` holds `EXTRACT_DAYS=4`.
So the candidate is a **copy** of that file, never a rebuild from scratch.

## Before you start

- modelgen released; this branch merged and checked out as `main`.
- Choose a Saturday, outside 01:30 to 05:00 UTC. The script refuses while
  `wraith-bus-build.service`, `wraith-bus-extract.service` or
  `wraith-bus-extract.path` is active, and checks again right before the swap
  (exit 4 if a build started while it waited for your `go`).
- Stop the extract timer and the extract path unit for the whole cutover, and
  start them again afterwards.
- **Record the pre-merge commit or tag BEFORE merging**, and export it:
  `CUTOVER_OLD_REF=<commit-or-tag> ops/cutover.sh`. After the merge the default
  `origin/main` is the new code, baseline equals candidate, and the compare would
  prove nothing; the script refuses to run when the two trees are identical.
- Stop osrm, backend and tiles (as for a full build). Keep the ingestor and
  QuestDB running. The preflight needs at least 13000 MB of available memory
  (the build unit's `MemoryMax`) and two warehouse copies plus 1 GB free beside
  the file (the warehouse was 6.0 GB on 2026-11-15) and one copy plus 1 GB free
  for the backup.
- Run as your own user, never root, and never with sudo.

## Run

```bash
ops/cutover.sh --dry-run      # preflight for real, echoes the other stages
ops/cutover.sh                # the cutover
```

Stages (each echoed, all logged to `ops/logs/cutover-<UTC stamp>/cutover.log`,
build output in `build-baseline.log` and `build-candidate.log`):

| Stage | What | Failure leaves |
| --- | --- | --- |
| 0 preflight | not root, no build running, memory, disk | nothing written |
| 1 backup | whole file to `/mnt/archive/build_db_backup/` | nothing changed |
| 2 copies | BASELINE (old code, `CUTOVER_OLD_REF`, the pre-merge commit) and CANDIDATE (this code), each a copy of the file, sharing `data/` | live file untouched |
| 3 build | `dbt run` in each copy | live file untouched |
| 4 compare | `ops/cutover_compare.py` on `eta_models`, `localities`, `line_punctuality`: row counts and every non-DOUBLE column exactly; DOUBLE columns within `1e-6 + 1e-9*|x|` (3-thread sums are not bit-reproducible) | live file untouched |
| 5 validate | offline `datacontract lint`, then `contracts/validate.py` on the candidate | live file untouched |
| 6 swap | hard-link the old file to `<file>.pre-cutover-<stamp>`, then one atomic `mv` | old file in place |

Stage 5's validate step reads live QuestDB. The script prints the command and
waits for you to type `go`; with `--no-prompt` it stops with exit 3. It never
runs it on its own. History tables ride in the copy and are never rebuilt.

A failed compare means the new code does not reproduce the old results. Read
the `MISMATCH` lines, fix on the branch, and rerun; nothing live has changed.

## After the swap

Three nightly builds on the ready event must pass with zero pages. Then delete
`dbt/cutover-work/` and the `.pre-cutover-*` file, and record the SQL ratchet
baseline (`ops/sql_ratchet.py`).

## Rollback

1. Stop the build unit and any scheduler trigger.
2. Check out the pre-cutover code (the commit before the merge).
3. Put the old file back: `mv -f <file>.pre-cutover-<stamp> dbt/wraith_bus_ingestor_build.duckdb`
   (same filesystem, atomic). If it is gone, restore the stamped file from
   `/mnt/archive/build_db_backup/` instead.
4. `rm -f dbt/wraith_bus_ingestor_build.duckdb.wal`, then run one normal build.

**Limit: rollback is lossless only within the extract window (`EXTRACT_DAYS=4`).**
The restored file holds history only up to the cutover. Days that ran on the new
code are not in it, and the old code can rebuild only what `dbt/data/` still
holds, the last 4 days. Beyond 4 days after the swap, the days in between are
lost from the segmentation and history tables unless re-extracted from the
Parquet archive (ADR-0007). So decide within 4 days, and rehearse the rollback
before relying on it.
