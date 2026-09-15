-- Guards the assumption int_stop_identity.sql's any_value() swap relies on:
-- unlike the count(*) vs count(distinct journey_key) fix elsewhere in this
-- project (a structural guarantee), this one is an EMPIRICAL fact about
-- current data -- measured 2026-09-15, 0 of 173,639 stops had more than one
-- distinct stop_name or locality. any_value() and mode() are only
-- equivalent while that holds. If NaPTAN ever genuinely renames a stop
-- mid-window, or ingestion introduces name drift, any_value() would start
-- silently returning an arbitrary pick instead of the most common one --
-- this test catches that instead of letting it ship quietly.
--
-- dbt tests fail on any row returned.

select
    atco_code,
    count(distinct stop_name) as distinct_names,
    count(distinct locality)  as distinct_localities
from {{ ref('int_pings_matched_to_stops') }}
group by atco_code
having count(distinct stop_name) > 1
    or count(distinct locality) > 1
