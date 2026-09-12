{#
  Which columns actually exist in data/bus_positions_*.parquet right now.

  The ingestor only started emitting the schedule columns (direction,
  journey_ref, origin_ref, dest_aimed_arr, ...) on 2026-09-12, so older daily
  parquet files have a genuinely different schema. read_parquet(union_by_name)
  papers over the case where SOME files have the column, but if NO file has it
  the column does not exist and every model referencing it fails to compile.

  This macro asks DuckDB once, at compile time, what the union actually
  contains; stg_bus_pings then emits `cast(null as ...) as journey_ref` for the
  ones that are missing. That keeps the project buildable on a fresh box that
  has only historical data.
#}
{% macro ping_columns() %}
  {%- if not execute -%}
    {{ return([]) }}
  {%- endif -%}
  {%- set results = run_query("select * from " ~ source('questdb', 'bus_positions') ~ " limit 0") -%}
  {{ return(results.column_names | map('lower') | list) }}
{% endmacro %}
