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
2. A fresh sandbox does not parse. `models/marts/_generated/_semantic.yml` needs a MetricFlow time spine model. The file was moved out of the sandbox copy only. The repo was not touched.

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

**Status:** NOT RUN. Needs the owner's go and the real data volume.
