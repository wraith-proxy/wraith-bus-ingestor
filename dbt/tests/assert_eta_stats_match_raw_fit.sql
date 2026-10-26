{#
  Pooling int_eta_daily_stats must give the same fit as DuckDB's own OLS
  aggregates over the raw training pings, and eta_models must pool it the
  same way.

  Why this exists: eta_models no longer sees a single ping. It pools the
  per-date sufficient statistics in int_eta_daily_stats (see the headers of
  both), and that is only worth anything if the pooled line IS the line
  regr_slope() would fit. A wrong date attribution, a dropped or double-counted
  cell, a sign slip in a centred term or regr_avgx/regr_avgy swapped would all
  still publish a plausible-looking model, and the speed and r2 tests would
  never notice.

  Two checks, unioned into one set of failing rows; `check_name` says which
  one a row comes from, `line` is the key ('*' = the whole network), and the
  expected_* / actual_* columns are the two sides.

  1. int_eta_daily_stats against the raw pings, on the newest dates:
     stats_date >= max(stats_date) - 2 days. Every run rewrites at least those
     (its own rewrite set is measured from the max BEFORE the run, which can
     only be the same or older), so they were written from the very pings now
     in the build. The fit is derived twice, per line and for the network:
       - expected: from int_pings_measured_to_journey_end with regr_count,
         regr_slope, regr_intercept and regr_r2, each row attributed to the
         local date of its journey's end -- re-derived here, not borrowed;
       - actual: from int_eta_daily_stats, pooled with this test's OWN copy of
         the formula -- deliberately not shared with eta_models, so one wrong
         edit cannot make both sides agree.

  2. eta_models against int_eta_daily_stats. Check 1 never reads eta_models,
     so a slip in its own pooling (the rung windows, the sums, the fit) would
     publish unseen. Its per-line rows (rung 10: line real, everything else
     '*') and its network row (rung 11, all '*') are the actual side; the
     expected side is this test's pooling of EVERY date in int_eta_daily_stats
     at those two grains, through the same sane filters eta_models applies
     (n_obs >= 200, positive slope, 3600 / slope between 3 and 90 km/h). A
     line that survives the filters on one side only fails too. The finer
     rungs are not compared.

  Counts must match exactly. Slope, intercept and r2 fail when exactly one side
  is NULL, when exactly one side is NaN, or when
  abs(a - b) > 1e-6 * greatest(1, abs(a), abs(b)). NaN needs its own rule
  because the tolerance cannot see it: regr_slope() returns NaN when
  remaining_km never varies, a NaN anywhere in the sums spreads to the fit, and
  a NaN on either side makes both sides of the > NaN, which DuckDB calls equal.
  The two sides add the same numbers in a different order, so they agree to
  rounding, not to the bit. The floor of 1 is for intercepts near zero, where
  rounding is a large fraction of a tiny value and a purely relative bar trips
  on noise; a formula bug moves a fit by far more than 1e-6.
#}

with since as (

    select max(stats_date) - interval '2 days' as since_date
    from {{ ref('int_eta_daily_stats') }}

),

stats as (

    -- Every daily row under its own line and again under '*' for the network
    -- fit: once in the pooling over every date (check 2), and once more, for
    -- the newest dates only, in the pooling check 1 compares with the pings.
    select 'all' as dates, line, n_obs, avg_km, avg_secs, sxx, sxy, syy
    from {{ ref('int_eta_daily_stats') }}

    union all

    select 'all', '*', n_obs, avg_km, avg_secs, sxx, sxy, syy
    from {{ ref('int_eta_daily_stats') }}

    union all

    select 'recent', line, n_obs, avg_km, avg_secs, sxx, sxy, syy
    from {{ ref('int_eta_daily_stats') }}
    where stats_date >= (select since_date from since)

    union all

    select 'recent', '*', n_obs, avg_km, avg_secs, sxx, sxy, syy
    from {{ ref('int_eta_daily_stats') }}
    where stats_date >= (select since_date from since)

),

means as (

    select
        dates,
        line,
        sum(n_obs)                         as n_obs,
        sum(n_obs * avg_km) / sum(n_obs)   as mean_km,
        sum(n_obs * avg_secs) / sum(n_obs) as mean_secs
    from stats
    group by dates, line

),

pooled as (

    select
        m.dates,
        m.line,
        m.n_obs,
        m.mean_km,
        m.mean_secs,
        sum(s.sxx + s.n_obs * (s.avg_km - m.mean_km) * (s.avg_km - m.mean_km))         as sxx,
        sum(s.sxy + s.n_obs * (s.avg_km - m.mean_km) * (s.avg_secs - m.mean_secs))     as sxy,
        sum(s.syy + s.n_obs * (s.avg_secs - m.mean_secs) * (s.avg_secs - m.mean_secs)) as syy
    from stats s
    join means m on m.dates = s.dates and m.line = s.line
    group by m.dates, m.line, m.n_obs, m.mean_km, m.mean_secs

),

pooled_fit as (

    -- Answering the degenerate groups the way regr_* does: SXX = 0 is a NaN
    -- slope (0 / 0) with a NULL intercept and r2, SYY = 0 is an r2 of 1.
    -- least() is only there for rounding.
    select
        dates,
        line,
        n_obs,
        sxy / sxx                                     as slope,
        mean_secs - sxy / nullif(sxx, 0) * mean_km    as intercept,
        case
            when sxx = 0 then null
            when syy = 0 then 1
            else least(1.0, sxy * sxy / (sxx * syy))
        end                                           as r2
    from pooled

),

raw_fit as (

    -- One scan, two fits: GROUPING SETS gives a row per line plus one for the
    -- whole network, whose line comes out NULL and is named '*' (line itself is
    -- never NULL in the training set).
    select
        coalesce(t.line, '*')                            as line,
        regr_count(t.remaining_secs, t.remaining_km)     as n_obs,
        regr_slope(t.remaining_secs, t.remaining_km)     as slope,
        regr_intercept(t.remaining_secs, t.remaining_km) as intercept,
        regr_r2(t.remaining_secs, t.remaining_km)        as r2
    from {{ ref('int_pings_measured_to_journey_end') }} t
    join {{ ref('int_journeys_shaped_and_directed') }} j on j.journey_key = t.journey_key
    where cast((j.end_ts at time zone 'UTC') at time zone 'Europe/London' as date) >= (select since_date from since)
    group by grouping sets ((t.line), ())

),

sane_fit as (

    -- eta_models' own filters, copied: what it would publish from these sums.
    select *
    from pooled_fit
    where dates = 'all'
      and n_obs >= 200
      and slope is not null
      and slope > 0
      and 3600.0 / slope between 3 and 90

),

published as (

    -- Rungs 10 and 11 are the only rows whose operator, direction, day_type
    -- and hour_band are all '*'; line is real on rung 10 and '*' on rung 11,
    -- the same key the pooling above gives the network.
    select
        line,
        n_obs,
        slope_s_per_km as slope,
        intercept_s    as intercept,
        r2
    from {{ ref('eta_models') }}
    where operator = '*'
      and direction = '*'
      and day_type = '*'
      and hour_band = '*'

),

pairs as (

    select
        'int_eta_daily_stats vs regr_* over the raw pings' as check_name,
        line,
        r.n_obs     as expected_n_obs,
        p.n_obs     as actual_n_obs,
        r.slope     as expected_slope,
        p.slope     as actual_slope,
        r.intercept as expected_intercept,
        p.intercept as actual_intercept,
        r.r2        as expected_r2,
        p.r2        as actual_r2
    from raw_fit r
    full outer join (select * from pooled_fit where dates = 'recent') p using (line)

    union all

    select
        'eta_models rungs 10-11 vs pooled int_eta_daily_stats' as check_name,
        line,
        s.n_obs,
        e.n_obs,
        s.slope,
        e.slope,
        s.intercept,
        e.intercept,
        s.r2,
        e.r2
    from sane_fit s
    full outer join published e using (line)

)

select *
from pairs
where expected_n_obs is distinct from actual_n_obs
{%- for col in ['slope', 'intercept', 'r2'] %}
   or (expected_{{ col }} is null) <> (actual_{{ col }} is null)
   or isnan(expected_{{ col }}) <> isnan(actual_{{ col }})
   or abs(expected_{{ col }} - actual_{{ col }}) > 1e-6 * greatest(1, abs(expected_{{ col }}), abs(actual_{{ col }}))
{%- endfor %}
