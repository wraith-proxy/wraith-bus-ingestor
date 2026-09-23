{#
  The GB ingest bounding box: mainland Great Britain, widened from the
  north-west of England on 2026-09-12 (see ROADMAP.md, "Decided, 2026-09-23
  -- keep the ingest bounding box GB-wide"). Repeated verbatim in
  stg_bods__vehicle_pings.sql and stg_naptan__stops.sql -- flagged as a macro
  candidate while writing the Learn dbt walkthrough (see ROADMAP.md 1.18.0).
#}
{% macro in_gb_bbox(lat_expr, lon_expr) -%}
({{ lat_expr }} between 49.8 and 61.0 and {{ lon_expr }} between -8.7 and 2.0)
{%- endmacro %}
