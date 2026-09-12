{#
  The search key shared by mart_localities, mart_line_localities and the Flask
  /api/search endpoint. NaPTAN locality names arrive as free text
  ("Ashton-under-Lyne", "St. Helens", "Newcastle  upon Tyne"), so a user typing
  "ASHTON UNDER LYNE" must land on the same key.

  Rule (frozen in CONTRACT.md s2): uppercase, strip everything that is not a
  letter/digit/space, collapse runs of spaces, trim.
#}
{% macro locality_norm(expr) -%}
trim(regexp_replace(
  upper(regexp_replace({{ expr }}, '[^A-Za-z0-9 ]', ' ', 'g')),
  ' +', ' ', 'g'
))
{%- endmacro %}
