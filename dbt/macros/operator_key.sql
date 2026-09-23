{#
  The pre-2026-09-12, no-operator sentinel. operator is genuinely NULL for
  every ping before that date, and SQL joins/partitions never match NULL to
  NULL, so every operator-grained join or window needs this coalesce or it
  silently loses every historical row. Repeated verbatim in
  int_pings_segmented_to_journeys.sql -- flagged as a macro candidate while
  writing the Learn dbt walkthrough (see ROADMAP.md 1.18.0).
#}
{% macro no_operator_sentinel(operator_expr) -%}
coalesce({{ operator_expr }}, '~none~')
{%- endmacro %}
