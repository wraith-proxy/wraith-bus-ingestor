{#
  Great-circle distance in kilometres between two lat/lon pairs.

  DuckDB has no PostGIS, and we only ever need distances of a few hundred
  metres up to a few tens of kilometres, so the plain haversine formula on a
  spherical earth (R = 6371 km) is both accurate enough (<0.5% error) and
  cheap enough to run tens of millions of times.

  Emitted inline as an expression -- Jinja macro rather than a SQL UDF, because
  a UDF would have to be registered on every DuckDB connection dbt opens.
#}
{% macro haversine_km(lat1, lon1, lat2, lon2) -%}
(
  2 * 6371.0 * asin(
    sqrt(
      pow(sin(radians(({{ lat2 }}) - ({{ lat1 }})) / 2), 2)
      + cos(radians({{ lat1 }})) * cos(radians({{ lat2 }}))
        * pow(sin(radians(({{ lon2 }}) - ({{ lon1 }})) / 2), 2)
    )
  )
)
{%- endmacro %}
