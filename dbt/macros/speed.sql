{#
  A journey's average speed in km/h: path distance over elapsed time.
  Repeated 3x verbatim in int_journeys_shaped_and_directed.sql before this
  macro existed (the value itself, plus its own plausibility filter twice) --
  flagged as a macro candidate while writing the Learn dbt walkthrough
  (see ROADMAP.md 1.18.0).
#}
{% macro avg_speed_kmh(path_km, duration_secs) -%}
({{ path_km }} / ({{ duration_secs }} / 3600.0))
{%- endmacro %}
