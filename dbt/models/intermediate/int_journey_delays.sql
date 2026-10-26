{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='stats_date',
    full_refresh=var('replay', false),
    post_hook=[] if var('replay', false) else ["delete from {{ this }} where stats_date < (select max(stats_date) from {{ this }}) - interval '{{ history_days() }} days'"]
) }}

{#
  One row per journey line_punctuality aggregates: how late it finished
  against its scheduled arrival, filed under the local date it finished on.

  These are line_punctuality's old journeys/delays CTEs, moved one layer down
  and made to persist. Computed straight off int_journeys_shaped_and_directed,
  the punctuality sample could only ever be as long as extract.py's window of
  raw pings (retention_days()), and that window is what ADR-0010 shrinks to
  free the SSD. One row per journey is tiny next to the pings it came from,
  so this table keeps history_days() local dates of them instead, and
  line_punctuality reads all of it.

  stats_date is the Europe/London local date of the journey's (trimmed)
  end_ts, by the same conversion stg_bods__vehicle_pings uses for
  ping_local_date. end_ts is a UTC wall clock with no zone attached, so it is
  read back as UTC first and then converted; the result is the
  ping_local_date of the journey's last moving ping, whatever the session
  time zone of the box. int_eta_daily_stats keys its rows by the same
  expression, so the two history tables rewrite and age out in step.
  day_type and hour_band stay the journey's own, from where it STARTED:
  stats_date only decides which night a row is (re)written on, it is not a
  grain of anything.

  ---------------------------------------------------------------------------
  Incremental strategy
  ---------------------------------------------------------------------------
  Same "partition replace" pattern as int_pings_segmented_to_journeys:
  delete+insert on stats_date deletes every existing row whose stats_date
  appears in the new batch, then inserts the batch. On an incremental run
  the batch is every journey with stats_date >= this table's own
  max(stats_date) - interval '2 days', that max being read when the run
  STARTS, i.e. the newest date last night wrote. A first build writes every
  date the ping table still has.

  Why 2 days. Surviving journeys last at most 5 hours, so a journey whose
  trimmed end moves across local midnight between two nights (the bus stood
  reporting past the extract's cut, then moved again the next night) has
  its old date and its new date in the rewrite set both times: the old row
  is deleted and the new one inserted, and the journey is never counted
  under two dates. And because the anchor is this table's own max, not the
  ping table's, a missed or failed night moves nothing: the next run starts
  from the same max and rewrites every date since, which heals the gap for
  as long as the raw window (retention_days()) still holds those dates. A
  date older than the rewrite set keeps what it was last written with.

  full_refresh=var('replay', false). A plain `dbt run --full-refresh`
  (build.sh FULL_REFRESH=1) leaves this table alone: a rebuild from the
  pings would silently throw away every date the pings no longer cover.
  ops/incident.py replay (FULL_REFRESH=1 REPLAY=1) sets var replay, which
  rebuilds the history from whatever is in data/, the same thing replay
  means for every other model here.

  The post-hook prunes to history_days() back from the newest stats_date,
  and is switched off for ops/incident.py replay the same way the
  segmentation one is.
#}

with journeys as (

    select
        journey_key,
        line,
        direction,
        day_type,
        hour_band,
        end_ts,
        dest_aimed_arr_ts,
        cast((end_ts at time zone 'UTC') at time zone 'Europe/London' as date) as stats_date
    from {{ ref('int_journeys_shaped_and_directed') }}
    -- direction is required/primaryKey in bus-marts.odcs.yaml, and
    -- line_punctuality has no '*'-direction fallback grain the way eta_models
    -- does -- so a journey the feed didn't tag (31.9% since the
    -- direction-inference heuristic was retired, ROADMAP.md 1.2.0) is
    -- excluded here rather than published as a NULL, same principle as
    -- eta_models' operator/direction guard.
    where dest_aimed_arr_ts is not null
      and direction is not null

),

delays as (

    select
        stats_date,
        journey_key,
        line,
        direction,
        day_type,
        hour_band,
        date_diff('second', dest_aimed_arr_ts, end_ts) as delay_s
    from journeys
    -- Anything beyond plus or minus two hours is a mis-joined schedule, not a
    -- late bus; a two-hour-late bus has been cancelled and replaced.
    where abs(date_diff('second', dest_aimed_arr_ts, end_ts)) <= 7200
    {% if is_incremental() %}
      -- Only last night's newest stats_date, the 2 dates before it and
      -- anything newer are ever (re)written; see the header.
      and stats_date >= (select max(stats_date) from {{ this }}) - interval '2 days'
    {% endif %}

)

select * from delays
