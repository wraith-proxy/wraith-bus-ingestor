# The Rules: how we do dbt here

Opinionated on purpose. dbt lets you do almost anything, which is exactly why
projects turn into swamps. These rules are why this one didn't. Each rule says
where you can see it in the repo.

## Structure

**1. Three layers. Always.** `staging/`, then `intermediate/`, then `marts/`. Data
flows one way, left to right, and never backwards.
*See it:* the folders under `dbt/models/`.

**2. Staging renames and casts. Nothing else.** No joins, no maths, no clever
filters. One staging model per source table, and it's a **view**.
*See it:* `stg_naptan__stops.sql` is a `select` of `cast(...) as ...` and one
sensible `where`.

**3. Marts are few, small and boring.** They are the shapes the app wants, and
nothing more. This project has **three**. If a mart is not read by something, it
does not exist.

**4. Intermediate is where the thinking goes.** Cut the stream into journeys, work
out direction, measure the time left. If a model is hard to read, split it into
two.

## Naming

**5. `stg_<source>__<thing>`.** Two underscores between source and thing:
`stg_bods__vehicle_pings`. You can tell where it came from at a glance.

**6. `int_<thing>_<verb-ed>`.** Say what happened to it:
`int_pings_segmented_to_journeys`, `int_journeys_shaped_and_directed`.

**7. Marts are plain nouns.** `localities`, `eta_models`. No `mart_` prefix, no
`dim_`, no `fct_`. The API reads these names; they are the product.

**8. One YAML per folder, starting with an underscore.** `_marts__models.yml` sorts
to the top and holds every description and test for that folder.

## Writing SQL

**9. Import with CTEs at the top, one final `select` at the bottom.** Every model
starts `with thing as (select * from {{ ref(...) }}),`. `select *` is fine to
*import* and to *finish* (`select * from final`). Everywhere in the middle, name
your columns.
*See it:* `localities.sql`.

**10. Lowercase. Short steps. Named steps.** A CTE called `by_locality` tells you
what it holds. A CTE called `cte2` is a crime.

**11. Comments say *why*, never *what*.** `avg(lat)` needs no comment. *Why we use
the centroid of the stops instead of the town centre* does. Read any real model
header for the standard.

**12. Every window function gets a TOTAL order.** If two rows can tie in your
`order by`, your build is a coin flip. This project's journey model had ties: two
builds of the *same data* disagreed on **16.8% of rows**. Now it orders by every
column it needs to, and two builds match exactly. Determinism is not optional.

## Materializations

**13. Views for staging, tables for marts.** It is set once in `dbt_project.yml`.
Don't override it in a model without a reason you can write in a comment.

**14. Incremental only when a full rebuild is too slow, and not before.** It is
harder to get right and it can drift. This project has **one** incremental model,
and it earned it: it took an hour.

**15. When you change an incremental model's logic, run `--full-refresh`.** Or
enjoy old rows built with old logic.

## Tests

**16. Test every key: `not_null` and `unique`.** If you don't know what makes a row
unique, you don't understand your model yet.

**17. Test the thing that would hurt if it silently broke.** Not everything.
The one row without which every bus loses its ETA gets a test.
*See it:* `tests/assert_eta_model_has_global_fallback.sql`.

**18. A failing test blocks the publish.** Yesterday's good data beats today's
wrong data. `dbt/build.sh` stops before it publishes. Never drop a test to `warn`
to get a green build. The three that *are* `warn` guard things we do not control
(the incoming feed), and each says why in a comment. If you cannot write that
comment, it stays an error.

## Living with it

**19. Never run `dbt` in `dbt/`.** That folder is the nightly's. Use the sandbox
(`ops/dbt_sandbox.sh`). Always.

**20. Delete models you do not need.** This project deleted **89 of 97 models**
when a downloaded timetable did the job better. Less code is fewer bugs and a
faster build. Deleting is a feature.

**21. Measure before you optimise.** The build went from an hour to fifteen minutes
because someone timed every step first and found one model was 85% of the cost.
Guessing would have fixed the wrong thing.

**22. If it needs a paragraph to explain, the paragraph goes in the file.** The
next person is you, in six months, at 3am.

!!! quote "The one-line version"
    Clean the data once (staging), think about it once (intermediate), serve it
    plainly (marts), test the keys, and delete whatever you don't use.
