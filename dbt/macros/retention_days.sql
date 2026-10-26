{% macro retention_days() %}
  {{ return(var('extract_days', 4)) }}
{% endmacro %}
