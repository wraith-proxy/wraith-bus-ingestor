# MetricFlow feasibility (dbt-core 1.12.4 + dbt-duckdb 1.11.0 + duckdb 1.5.5)
Scratch venv: .../scratchpad/mf_check (py3.11). Nothing executed beyond uv install and file reads.

1. Package: `metricflow` 0.213.0 (engine + semantic interfaces, bundled as metricflow_semantic_interfaces/ and metricflow_semantics/)
   and `dbt-metricflow` 0.15.0 (the `mf` CLI; Requires: dbt-core<1.13,>=1.11, metricflow==0.213.0, click, halo, jinja2, update-checker).
   - dbt-core 1.12.4 METADATA: `Requires-Dist: metricflow<1.0,>=0.211.0`. uv.lock line 1106 already has metricflow 0.213.0 -> parsing
     semantic_models/metrics is ALREADY in the project venv; only the `mf` CLI (dbt-metricflow) is new. [high]
   - `uv pip install --dry-run dbt-core==1.12.4 dbt-duckdb==1.11.0 duckdb==1.5.5 dbt-metricflow` -> resolves, no conflict; real install 66 pkgs. [high]
   - metricflow dev extra pins duckdb<1.6,>=1.5 (consistent with 1.5.5 and project's duckdb<2.0). [high]
2. DuckDB engine: yes. dbt_metricflow/cli/dbt_connectors/adapter_backed_client.py:52 `DUCKDB = "duckdb"`, :72 -> SqlEngine.DUCKDB,
   :96 -> DuckDbSqlPlanRenderer(); metricflow/sql/render/duckdb_renderer.py (class DuckDbSqlExpressionRenderer line 30). `mf` runs
   queries through the dbt adapter (so through dbt-duckdb's connection/profile). [high]
3. Minimal YAML (schemas.py in metricflow_semantic_interfaces/parsing; additionalProperties:false everywhere, so strict):
   semantic_model: required `name`; dbt-core adds required `model: ref('mart')` (dbt/artifacts/resources/v1/semantic_model.py:180).
     entities[] required name,type (PRIMARY|UNIQUE|FOREIGN|NATURAL); measures[] required name,agg (SUM,MIN,MAX,AVERAGE,COUNT_DISTINCT,SUM_BOOLEAN,COUNT,PERCENTILE,MEDIAN),
     optional expr, agg_time_dimension; dimensions[] required name,type (CATEGORICAL|TIME); time dims require type_params.time_granularity.
     defaults.agg_time_dimension optional.
   metric: required name, type (SIMPLE|RATIO|CUMULATIVE|DERIVED|CONVERSION), type_params (simple: measure: <name>).
   Sketch:
     semantic_models:
       - name: eta_mart
         model: ref('eta_mart')
         defaults: {agg_time_dimension: observed_at}
         entities: [{name: trip, type: primary, expr: trip_id}]
         measures: [{name: delay_s_sum, agg: sum, expr: delay_s}]
         dimensions: [{name: observed_at, type: time, type_params: {time_granularity: hour}}, {name: route, type: categorical}]
     metrics:
       - {name: total_delay, type: simple, type_params: {measure: delay_s_sum}}
   Plain deterministic YAML, trivially generatable (JSON-schema validated; same style as existing generate_line_stops_batches.py). [high]
   Time-based queries (metric_time) need a time spine model (models config `time_spine`, dbt/parser/schemas.py:781+). [medium: mechanism read, not tested]
   Exact whole-schema review of rarely used fields not done.
4. Footprint: scratch site-packages 159M total for this stack; biggest = _duckdb .so 58M (already present), babel 33M, rapidfuzz 12M, networkx 8M,
   sqlglot 3.5M. Project .venv is 546M. New vs lock for dbt-metricflow extra: halo, spinners, log-symbols, update-checker, termcolor(+colorama?) ~ small (<5M). [medium on exact delta]
   Runtime: mf compiles SQL in Python and the SQL runs in DuckDB; metricflow plans are Python-heavy but small for few metrics. Memory of a query is DuckDB's;
   project has OOM history (duckdb memory_limit), so mf queries must respect the same memory_limit / not run beside the build. [medium]
   dbt-metricflow may phone home (snowplow/update-checker) on mf use; dbt-core already has snowplow-tracker. [low, not verified]
5. Blockers: none found for resolution/parsing/duckdb support. Notes:
   - dry-run resolved pydantic 2.14.3, sqlglot 30.31.0 vs lock 2.13.5 / 30.18.0 (fresh resolve, not lock-constrained; metricflow only needs pydantic<3, sqlglot>=20) [high]
   - `mf` against a DuckDB file takes the file lock; conflicts with ingestor/dbt build/API readers (single-writer). [medium, DuckDB general knowledge]
   - semantic_manifest.json is produced by dbt parse/build into target/; adding semantic_models changes manifest/docs output. [medium]
   - DuckDB 1.5.5 vs metricflow dev pin <1.6: OK.

Could not determine: actual `mf query` correctness on this schema (not run by rule); real memory use; exact delta installed vs uv.lock (dry-run is fresh resolve);
time-spine requirement details for this project; whether any marts have a suitable PRIMARY/FOREIGN entity/time grain; telemetry behaviour of mf.
