# Roadmap

Where this is now, and what "done" (1.0.0) looks like. Not a schedule --
just the order things need to happen in, revised as the plan changes.

## Now: 0.2.x

A live, single-NAS deployment. Ingest, storage, transform and API all
work end to end, verified against real traffic, split out of `ghost-site`
into its own repo with full git history.

`ghost-runner` (Alpine/musl) got `duckdb` baked in (`ci-images`,
2026-09-14) -- compiled from source once at image build instead of every
CI run, since `duckdb` ships no musllinux wheel. `test.yml` is back to
`uv sync --all-extras` and the "Validate data contracts" step is restored.

`dbt-core` is deliberately NOT baked into `ghost-runner`: one of its own
hard deps, `dbt-core-experimental-parser`, ships no musllinux wheel and
refuses to build from source at all, on any platform, without a matching
prebuilt binary -- no toolchain fixes that. Not needed there anyway --
`dbt docs generate` already runs on `ubuntu-latest` (glibc) in `pages.yml`.

## 0.3.0 -- own the data, not borrow it

`maps/`, `questdb_data/` and `osrm_data/` are still physically inside
`ghost-site/`, referenced here by absolute path (`docker-compose.yml`'s
`*_PATH` env vars) because QuestDB was live mid-ingest during the split and
moving them wasn't safe to do at the same time. `wraith-bus-ingestor` is
what actually owns and writes all three -- they belong here. A maintenance
window: stop the containers, `mv` (same filesystem, so this is fast), point
the env vars at the new local paths, restart.

## 0.4.0 -- retire the direction heuristic

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
gated, not just pytest) and no repo-local absolute paths reaching into
`ghost-site` anywhere. Contract versions (`bus-marts.odcs.yaml`,
`bus-positions.odcs.yaml`) are already at `2.0.0` from the September
restructure -- 1.0.0 here is a commitment to no more breaking changes
without a major bump, not a version reset.
