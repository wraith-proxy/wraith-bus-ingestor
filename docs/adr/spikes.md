# Spikes

Results of short experiments run in a throwaway dbt sandbox. The repo itself was not modified by any spike.

## Spike (a) dbt contract enforcement

**Question:** With a contract enforced, does a deliberate column mismatch fail the build and name the column, for view, table and incremental models?

**Result:** Passed.

**Observed:** All three model types fail at compile time with a message naming the column.
- Missing column: `missing_col`, reported as "missing in definition".
- Type mismatch: `name`, VARCHAR vs INTEGER, reported as "data type mismatch".
- The same behaviour held for view, table and incremental.

**Versions:** dbt 1.12.4, duckdb 1.11.0.

**Commands:**
- `ops/dbt_sandbox.sh ~/dbt-sandbox-54a`
- `dbt run --select spike_view`
- `dbt run --select spike_table`
- `dbt run --select spike_incremental`

**Caveats:**
1. An incremental model with an enforced contract needs `on_schema_change` set to `append_new_columns` or `fail`. Without it, the run hits a parse error.
2. A fresh sandbox does not parse. `models/marts/_generated/_semantic.yml` needs a MetricFlow time spine model. The file was moved out of the sandbox copy only. The repo was not touched. Resolved (#55): generated file now lives at `dbt/semantic/_semantic.yml`, outside `model-paths`, until the semantic layer is switched on with a time spine.

## Spike (b) incremental schema change without full refresh

**Question:** Adding a column to an incremental model with `on_schema_change=append_new_columns`, run without `--full-refresh`, adds the column and leaves existing history rows unchanged?

**Result:** Passed.

**Observed:**
- Before: columns `[id, val, n]`, rows `(1,a,10)`, `(2,b,20)`, `(3,c,30)`.
- Added `newcol` to the model and ran an incremental `dbt run` (no `--full-refresh`). It inserted new row `id 4`.
- After: columns `[id, val, n, newcol]`, rows `(1,a,10,NULL)`, `(2,b,20,NULL)`, `(3,c,30,NULL)`, `(4,d,40,x)`.
- The three history rows kept their count and values, with NULL in the new column.

**Versions:** dbt 1.12.4, DuckDB adapter.

**Commands:**
- `ops/dbt_sandbox.sh ~/dbt-sandbox-54b`
- `dbt run -s spike_inc` (v1, 3 rows)
- `duckdb: select * from main.spike_inc` (before)
- `dbt run -s spike_inc` (v2, adds `newcol`)
- `duckdb: select * from main.spike_inc; describe` (after)

**Caveats:**
1. A standalone toy model was used, `models/spike54b/spike_inc.sql`, in the sandbox only.
2. The DuckDB adapter was used, not the real warehouse.
3. The sandbox's `models/marts/_generated/_semantic.yml` was deleted because dbt parse failed on a missing MetricFlow time spine.

## Spike (c) MetricFlow peak memory

**Question:** What is MetricFlow's peak memory on a copy of the real build at real data volume?

**Result:** Peak 216 MiB (cgroup, whole run). Per-command max RSS 175 to 255 MiB. Far below the 13 GB cap.

**Observed (2026-11-15, 06:03 to 06:12 UTC, ops/spike_mf_memory.sh):**
- Copy of the real `wraith_bus_ingestor_build.duckdb` (6.0 GB). Marts are small: localities 25,343 rows, line_punctuality 27,630, eta_models 115,389. Memory is Python and DuckDB overhead, not data volume.
- `mf validate-configs`: 0 errors on semantic models, dimensions, entities, measures, metrics against the real warehouse copy. 175 MiB.
- `mf query` journey_count by line, eta_observations by operator, eta_observations by operator, line, direction and hour_band (tens of thousands of rows returned), locality_count with locality_stop_count: all succeed in under 0.1 s each. Max RSS 175 to 255 MiB.
- Ran as a systemd user service, MemoryMax 13000M, swap off. CPU time 17.6 s. No containers stopped.

**Finding for ADR-0011 (tickets #56, #57):** MetricFlow needs (1) a time spine model and (2) an `agg_time_dimension` on every measure, or `dbt parse` / validation fails. The marts have no time column, so the spike adds a day-grain time spine and a constant time dimension (`cast('2000-01-01' as date)`) to the sandbox COPY only. Neither is in the repo. The generated `_semantic.yml` lives in `dbt/semantic/`, outside `model-paths`, until the semantic layer is switched on.

**Caveats:**
1. The constant time dimension makes time-based queries meaningless. Only non-time metric queries were exercised.
2. Memory was measured with the ingestor, QuestDB and other containers running, not in the build window.
3. Versions: dbt-core 1.12.4, dbt-duckdb 1.11.0, duckdb 1.5.5, dbt-metricflow latest at run time.
