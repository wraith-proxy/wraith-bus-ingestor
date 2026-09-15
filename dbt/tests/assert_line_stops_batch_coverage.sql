-- Catches a batching bug in models/intermediate/int_line_stops_<grain>.sql
-- (a bug in dbt/scripts/generate_line_stops_batches.py's config or template
-- that mis-partitions one grain's batches) that DROPS or DUPLICATES an
-- entire line's groups. The existing assert_line_stops_grain_is_unique.sql
-- only catches DUPLICATED rows surviving all the way to the published mart;
-- a dropped line produces fewer rows, which passes a uniqueness check
-- trivially and would otherwise ship unnoticed.
--
-- Each grain's expected group count is recomputed independently, straight
-- from int_first_visit_per_journey with that grain's own filter/group-by,
-- reusing NONE of the batch files' own WHERE-clause logic -- so a bug
-- shared identically across all of one grain's generated batch files (the
-- failure mode this test exists for) can't also be present here and cancel
-- out. dbt tests fail on any row returned.
--
-- Boundary this test does NOT cover (named, not hidden): it would not catch
-- one line's groups being dropped and a different, equal-sized line's
-- groups being duplicated in the same run, netting to a zero diff. That
-- would require both a drop and an unrelated duplication to coincide
-- exactly in group count -- not a realistic failure mode for a
-- hash(line)-based partition, but worth knowing this test's precise
-- guarantee rather than treating it as absolute.

with expected as (

    select 'real_real' as grain, count(*) as expected_groups from (
        select distinct operator, line, direction, day_type, atco_code
        from {{ ref('int_first_visit_per_journey') }}
        where operator is not null
    )

    union all

    select 'real_star', count(*) from (
        select distinct operator, line, direction, atco_code
        from {{ ref('int_first_visit_per_journey') }}
        where operator is not null
    )

    union all

    select 'star_real', count(*) from (
        select distinct line, direction, day_type, atco_code
        from {{ ref('int_first_visit_per_journey') }}
    )

    union all

    select 'star_star', count(*) from (
        select distinct line, direction, atco_code
        from {{ ref('int_first_visit_per_journey') }}
    )

),

actual as (

    select 'real_real' as grain, count(*) as actual_groups from {{ ref('int_line_stops_real_real') }}
    union all
    select 'real_star', count(*) from {{ ref('int_line_stops_real_star') }}
    union all
    select 'star_real', count(*) from {{ ref('int_line_stops_star_real') }}
    union all
    select 'star_star', count(*) from {{ ref('int_line_stops_star_star') }}

)

select
    e.grain,
    e.expected_groups,
    a.actual_groups,
    e.expected_groups - a.actual_groups as groups_missing_or_extra
from expected e
join actual a using (grain)
where e.expected_groups != a.actual_groups
