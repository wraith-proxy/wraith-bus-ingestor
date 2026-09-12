{#
  Bucket an hour-of-day integer (0-23) into the five bands the ETA regression
  is fitted per. The bands are the standard UK service-pattern split: buses
  behave completely differently in a peak than at 02:00, and fitting one
  slope across all of them would average a 12 km/h peak crawl together with a
  40 km/h night run and predict neither.

  THE HOUR MUST BE LOCAL (Europe/London), NOT UTC. Every ping is stored in UTC,
  and the UK runs UTC+1 from late March to late October -- so for most of the
  year a UTC hour is an hour early, and 'am_peak' bucketed on UTC would really
  be 07:00-10:59 local. That is not a rounding detail: it shifts the entire
  peak/off-peak split against the traffic it is supposed to describe, and it
  puts the morning rush an hour out. stg_bus_pings therefore derives
  ping_local_ts with AT TIME ZONE 'Europe/London' and bands that.

  Kept as a macro so staging and any ad-hoc analysis agree on the boundaries.
#}
{% macro hour_band(hour_expr) -%}
case
  -- FROZEN: backend/app.py buckets "now" with exactly these cut points to look
  -- up mart_eta_model. If the two ever disagree, every lookup silently misses
  -- its band and falls through to the coarse '*' row, and the per-band fits
  -- become dead weight. Change both or neither.
  when {{ hour_expr }} < 6  then 'night'     -- 00:00-05:59
  when {{ hour_expr }} < 10 then 'am_peak'   -- 06:00-09:59
  when {{ hour_expr }} < 16 then 'inter'     -- 10:00-15:59 interpeak
  when {{ hour_expr }} < 19 then 'pm_peak'   -- 16:00-18:59
  when {{ hour_expr }} < 22 then 'evening'   -- 19:00-21:59
  else 'night'                               -- 22:00-23:59
end
{%- endmacro %}
