# Roadmap

Where this is now, and what "done" (1.0.0) looks like. Not a schedule --
just the order things need to happen in, revised as the plan changes.

## Now: 0.1.x

A live, single-NAS deployment. Ingest, storage, transform and API all
work end to end, verified against real traffic, split out of `ghost-site`
into its own repo with full git history. CI runs pytest; nothing else yet
(see below).

## 0.2.0 -- a real CI runner

`ghost-runner`'s current image is Alpine/musl. Neither `duckdb` (no
musllinux wheel, no C++ compiler to build one) nor `dbt-core` (same, via
`dbt-core-experimental-parser`) can install there, so CI dropped to pytest
only -- the "Validate data contracts" gate and `dbt docs generate` in CI
both need a runner that actually has these.

Fix: a custom `ghost-runner` image (glibc-based, or Alpine with the
duckdb/dbt wheels pre-built and baked in) so `uv sync --all-extras` just
works there like everywhere else. Once that lands:
- restore the contract-validation step to `test.yml`
- `dbt docs generate` in CI gets a real warehouse, not an empty one --
  catalog data in the published dbt docs stops being sparse

## 0.3.0 -- an installable CLI

The actual PyPI-installable unit. Not the whole platform -- that's a live
pipeline coupled to this NAS's paths, container names and QuestDB instance,
not something a stranger can `pip install` and run. What *is* generally
useful on its own:

- query BODS SIRI-VM directly (the thing `ingestor/ingest.py` already
  knows how to do, exposed as a standalone command instead of a poll loop)
- validate an ODCS contract or ontology file against a target (already
  exists as `contracts/validate.py` -- wrap it as a real entry point)

Ships as `0.0.1` first, loudly WIP, to claim the name and set expectations
honestly. Needs: picking the actual installable surface, a `src/` layout
for it (the rest of the repo stays sibling app dirs), flipping
`tool.uv.package = false` off for just that piece, a real entry point.

## 0.4.0 -- own the data, not borrow it

`maps/`, `questdb_data/` and `osrm_data/` are still physically inside
`ghost-site/`, referenced here by absolute path (`docker-compose.yml`'s
`*_PATH` env vars) because QuestDB was live mid-ingest during the split and
moving them wasn't safe to do at the same time. `wraith-bus-ingestor` is
what actually owns and writes all three -- they belong here. A maintenance
window: stop the containers, `mv` (same filesystem, so this is fast), point
the env vars at the new local paths, restart.

## 0.5.0 -- retire the direction heuristic

`DirectionRef` started arriving in the real feed on 2026-09-12.
`int_journeys_shaped_and_directed`'s bearing/shoelace inference exists only
for historical data and feed gaps -- once enough real `DirectionRef`-backed
data has accumulated, measure how often the heuristic and the real field
actually disagree, and simplify or retire whichever side loses.

## Smaller, whenever

- `dbt/macros/_macros.yml` argument types (`SQL expression`) aren't in
  dbt's valid-type set -- cosmetic warning on every `dbt docs generate`,
  never blocks anything, just noise worth silencing.
- Ingestor/archiver have no backoff on a genuinely dead feed beyond a flat
  60s retry -- fine at current reliability, worth revisiting if that stops
  being true.

## 1.0.0

Everything above done, plus: full CI (tests + contracts + dbt build all
gated, not just pytest), the CLI has real users other than this NAS, and
no repo-local absolute paths reaching into `ghost-site` anywhere. Contract
versions (`bus-marts.odcs.yaml`, `bus-positions.odcs.yaml`) are already at
`2.0.0` from the September restructure -- 1.0.0 here is a commitment to no
more breaking changes without a major bump, not a version reset.
