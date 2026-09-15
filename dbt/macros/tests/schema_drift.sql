{#
  dbt_expectations' expect_column_values_to_be_of_type and
  expect_table_columns_to_match_set both build `... from (describe {{ model }})`
  -- valid for a real table/view, but not for a source whose model resolves to
  a bare table-valued-function call (`read_parquet(...)`, no `select *`), which
  is exactly what this project's external_location sources are. Confirmed by
  hand (dbt test, 2026-09-15): every such test errors with
  "Parser Error: syntax error at or near '('" before it ever runs.

  These two replace them, using the query shape confirmed to actually work
  against a table-valued-function source: `describe select * from ...`, not
  `describe ...` alone.
#}

{% test castable_as(model, column_name, type) %}
-- Matches stg_bods__vehicle_pings' own try_cast philosophy: a value that
-- cannot be cast is the failure this guards against, not a catalog-reported
-- type (which union_by_name can quietly promote across files anyway).
-- dbt counts ROWS returned as failures, not a count() value -- select the
-- offending rows themselves, zero of them on a pass.
select {{ column_name }}
from {{ model }}
where {{ column_name }} is not null
  and try_cast({{ column_name }} as {{ type }}) is null
{% endtest %}


{% test columns_match_set(model, column_list) %}
with actual as (
    select column_name
    from (describe select * from {{ model }})
),
expected as (
    {% for col in column_list %}
    select '{{ col }}' as column_name
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)
select column_name from actual
where column_name not in (select column_name from expected)
union all
select column_name from expected
where column_name not in (select column_name from actual)
{% endtest %}
