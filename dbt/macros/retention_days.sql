{% macro retention_days() %}
  {{ return(var('extract_days', 8)) }}
{% endmacro %}
