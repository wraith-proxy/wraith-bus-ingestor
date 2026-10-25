{#
  Bucket a LOCAL timestamp into the service pattern the operator runs that day.

  Saturday and Sunday bus operation differs from Monday-Friday in both
  frequency and running time -- fewer journeys, fewer stops actually served,
  lighter traffic -- so fitting them together biases every grain. This is the
  third dimension of the ETA model's grain, alongside line/direction and
  hour_band.

  TAKES A LOCAL TIMESTAMP, NOT A UTC ONE. See macros/hour_band.sql for why that
  distinction is load-bearing; the short version is that a 00:30 Saturday ping
  is stored as 23:30 Friday UTC and would file under 'weekday'.

  isodow() is 1 = Monday .. 7 = Sunday.

  LIMITATION: bank holidays run a Sunday-ish service and are filed here as
  'weekday'. That is deliberate -- a holiday calendar is a whole extra source to
  maintain and get wrong, and a holiday is one day among the eight
  (history_days) the ETA model is fitted over. It does mean an ETA on Easter
  Monday will be slightly pessimistic.
#}
{% macro day_type(local_ts_expr) -%}
case isodow({{ local_ts_expr }})
  when 6 then 'sat'
  when 7 then 'sun'
  else 'weekday'
end
{%- endmacro %}
