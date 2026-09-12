{#
  Initial great-circle (compass) bearing in degrees, 0-360, from point 1 to
  point 2. Used to infer a journey's direction of travel when the feed does not
  give us DirectionRef.
#}
{% macro bearing_deg(lat1, lon1, lat2, lon2) -%}
(
  (degrees(atan2(
     sin(radians(({{ lon2 }}) - ({{ lon1 }}))) * cos(radians({{ lat2 }})),
     cos(radians({{ lat1 }})) * sin(radians({{ lat2 }}))
       - sin(radians({{ lat1 }})) * cos(radians({{ lat2 }}))
         * cos(radians(({{ lon2 }}) - ({{ lon1 }})))
  )) + 360) % 360
)
{%- endmacro %}

{#
  Smallest absolute angle between two compass bearings, 0-180 degrees.
  Plain subtraction is wrong across the 0/360 seam: 350 deg and 10 deg are 20
  deg apart, not 340.
#}
{% macro bearing_delta(b1, b2) -%}
abs((((({{ b1 }}) - ({{ b2 }})) + 180 + 360) % 360) - 180)
{%- endmacro %}
