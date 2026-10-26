{% macro history_days() %}
  {{ return(var('history_days', 8)) }}
{% endmacro %}
