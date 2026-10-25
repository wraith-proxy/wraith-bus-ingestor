{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='stats_date',
    full_refresh=var('replay', false),
    post_hook=[] if var('replay', false) else ["delete from {{ this }} where stats_date < (select max(stats_date) from {{ this }}) - interval '{{ history_days() }} days'"]
) }}

{#
  The ETA regression's training set, boiled down to the six numbers a
  least-squares fit actually needs, one local date at a time -- so eta_models
  can keep training on history_days() of evidence after the raw pings behind
  it have aged out of the SSD.

  ---------------------------------------------------------------------------
  Why six numbers are enough
  ---------------------------------------------------------------------------
  eta_models used to run regr_slope/regr_intercept/regr_r2/regr_count straight
  over int_pings_measured_to_journey_end, one row per training ping, and that
  was the only reason 8-9 days of raw pings were kept at all. A straight-line
  fit does not need the pings. Per group it needs:

      n_obs     how many (remaining_km, remaining_secs) pairs   regr_count
      avg_km    their mean remaining_km                         regr_avgx
      avg_secs  their mean remaining_secs                       regr_avgy
      sxx       sum of (km - avg_km)^2                          regr_sxx
      sxy       sum of (km - avg_km) * (secs - avg_secs)        regr_sxy
      syy       sum of (secs - avg_secs)^2                      regr_syy

  These are sufficient statistics: the six numbers of two groups combine into
  the six numbers of their union without looking at a single row again (the
  pairwise update of Chan, Golub & LeVeque, the standard way to merge partial
  variances). eta_models does that combination per rung, so every fallback is
  still a genuine refit at its own width, not an average of finer slopes. The
  sums are kept CENTRED, about each group's own mean, rather than as raw
  sum(x), sum(x^2), sum(x*y): sum(x^2) - n * mean^2 cancels catastrophically
  in floating point, and centred sums do not.
  dbt/tests/assert_eta_stats_match_raw_fit.sql re-proves, every night, that
  pooling these rows per line and network-wide gives the fit regr_* gives
  over the raw pings, on recent dates whose raw pings are still on hand.

  ---------------------------------------------------------------------------
  Grain, and which date a ping belongs to
  ---------------------------------------------------------------------------
  One row per (stats_date, operator, line, direction, day_type, hour_band):
  eta_models' finest grain, plus the date. operator, direction, day_type and
  hour_band are taken per training ping exactly as
  int_pings_measured_to_journey_end has them -- the band and day of THIS ping,
  the operator and direction of its journey. A NULL operator or direction is
  its own group, as GROUP BY always makes it; eta_models' null guards decide
  which rungs may use it, the same as when it read the pings.

  stats_date is the LOCAL date of the journey's END, not of each ping. A
  journey has to be counted under exactly one date, and it is only whole once
  it has ended: one that leaves at 23:50 and arrives at 00:40 belongs, whole,
  to the later date. end_ts is a naive UTC timestamp (stg_bods__vehicle_pings
  makes ping_ts with `ping_tstz at time zone 'UTC'`), so `at time zone 'UTC'`
  turns it back into the instant and `at time zone 'Europe/London'` into the
  local wall clock: the exact inverse of the staging conversion, which makes
  stats_date the ping_local_date of the journey's last ping. Both conversions
  are explicit, so the session time zone of the build box cannot move a
  journey across a date, and the BST/GMT change days come out right.

  ---------------------------------------------------------------------------
  Incremental strategy
  ---------------------------------------------------------------------------
  The first build writes every date the training set covers. After that a run
  rewrites stats_date >= max(stats_date) - 2 days, with the max read from
  {{ this }} as it stood BEFORE the run: last night's newest date, the two
  before it, and anything newer tonight. That is the same "anchor on the
  table's own pre-run max" pattern as int_pings_segmented_to_journeys, for
  two reasons.

  A journey can change date between two nights. int_journeys_shaped_and_directed
  drops every journey ending near a coverage edge until a later run sees it
  finish (see that model's header), and once more data arrives the trimmed end
  of a journey seen near the edge can move across midnight, to the next date.
  Journeys last at most 5 hours, so such a move only ever touches the newest
  dates, and two days behind last night's max puts both its old date and its
  new one in the rewrite set on both nights: the old count is deleted with its
  date and the journey is never counted under two dates.

  And a missed or failed night heals itself. Nothing was written, so the max
  has not moved, and the next run's rewrite set reaches back over every date
  the missed night should have rewritten, as long as the raw window still
  holds those dates. Older dates are left as they were written, which is the
  point: their raw pings may already be gone.

  `full_refresh=var('replay', false)` makes a plain --full-refresh, and
  therefore build.sh FULL_REFRESH=1, leave this table alone: it is the only
  copy of days whose raw pings no longer exist, so an ordinary rebuild must
  not be able to wipe it. A replay (ops/incident.py, which build.sh turns into
  the `replay` var) is the one exception: it rebuilds this table from whatever
  is in data/, as replay does for every other model, so afterwards the history
  covers only the days data/ holds. To start over without a replay, drop the
  table by hand.

  `delete+insert` on unique_key='stats_date' is the same "partition replace"
  pattern as int_pings_segmented_to_journeys. The post-hook keeps the newest
  date plus history_days() behind it (macros/history_days.sql), and is
  switched off by the `replay` var exactly like the segmentation model's.
#}

with journeys as (

    -- end_ts lives here, not in the training set. Every training row's journey
    -- is in this table by construction (int_pings_measured_to_journey_end is
    -- built from the same join), so the inner join below drops nothing.
    select
        journey_key,
        cast((end_ts at time zone 'UTC') at time zone 'Europe/London' as date) as stats_date
    from {{ ref('int_journeys_shaped_and_directed') }}

),

training as (

    select
        j.stats_date,
        t.operator,
        t.line,
        t.direction,
        t.day_type,
        t.hour_band,
        t.remaining_km,
        t.remaining_secs
    from {{ ref('int_pings_measured_to_journey_end') }} t
    join journeys j on j.journey_key = t.journey_key
    {% if is_incremental() %}
    where j.stats_date >= (select max(stats_date) from {{ this }}) - interval '2 days'
    {% endif %}

),

final as (

    select
        stats_date,
        operator,
        line,
        direction,
        day_type,
        hour_band,
        regr_count(remaining_secs, remaining_km) as n_obs,
        regr_avgx(remaining_secs, remaining_km)  as avg_km,
        regr_avgy(remaining_secs, remaining_km)  as avg_secs,
        regr_sxx(remaining_secs, remaining_km)   as sxx,
        regr_sxy(remaining_secs, remaining_km)   as sxy,
        regr_syy(remaining_secs, remaining_km)   as syy
    from training
    group by stats_date, operator, line, direction, day_type, hour_band

)

select * from final
