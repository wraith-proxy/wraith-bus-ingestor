# Landing: how the real nightly build uses all of this

You now know every piece the real project uses. Here is how they fit together at
03:00 every night, in plain words. The script is `dbt/build.sh`; open it alongside.

## The night shift

1. **Fetch.** A Python script (`extract.py`) asks QuestDB, the database that
   collects live bus positions all day, for each of the last eight days it does
   not already have (normally just yesterday). It saves them as Parquet files in
   `data/`. These are the **sources** from stop 3. This used to be the slowest
   part, about ten of the fifteen minutes, when it fetched every day every night.
2. **Build.** `dbt run`: all 10 models, in dependency order (stop 3), each as the
   view, table or incremental table it is configured to be (stop 5). About two
   minutes.
3. **Check.** `dbt test`: all 88 tests (stop 4). If **any** fail, the build stops
   here and nothing is published. The map keeps showing the previous night's good
   answer rather than a wrong one.
4. **Publish.** Only the three finished marts are copied into a fresh file, which
   is then swapped into place in one instant, so the API never sees a half-written
   table.
5. **Double-check.** A separate contract check confirms the published tables still
   have the shape the API expects.

## Why two database files?

The real build has *two*: a **build warehouse** that persists between nights (that
is what makes the incremental model fast, from stop 5) and a small **published
file** the API reads. Building in one place and swapping the result into another
means the API never reads something half-finished.

!!! danger "That is why you never run dbt in `dbt/`"
    Running dbt there means opening the build warehouse. If the nightly starts
    while you have it open, the nightly fails. The sandbox has its own copy, so
    you can never do this by accident.

!!! tip "Opinion"
    The one habit that matters: **build, test, then publish, in that order, and stop
    at the first failure.** Everything else in this course serves that.

## Where to go next

Some good next steps, roughly in order of difficulty:

1. **Read a whole real model.** Start with `models/marts/eta_models.sql`. It fits a
   straight line through bus journeys to predict arrival times. You can now follow
   every part of it except the statistics.
2. **Read the story of a real decision.** [ADR-0009](../adr/0009-fast-nightly-build.md)
   is how the build went from an hour to a few minutes, including what was
   measured and what went wrong on the way.
3. **Find out why models were deleted.** [ADR-0008](../adr/0008-published-timetables-vs-inferred-routes.md)
   explains why 89 of the project's 97 models could be replaced by a downloaded
   timetable. Sometimes the best model is no model.
4. **Add a real feature in your sandbox.** For example, a mart that counts
   journeys per line from `int_journeys_shaped_and_directed`, with a test for it.

## Challenges

Try these in the sandbox. Answers are hidden in the boxes.

??? question "1. Are there any locality names that appear twice in `localities`?"
    Trick question. `locality_norm` is unique by construction (there is a test for
    it), so every row is a different normalised name:

    ```bash
    dbt show --inline "select locality_norm, count(*) c from {{ ref('localities') }} group by 1 having c > 1"
    ```

    returns nothing. Why is that guaranteed? Look at the `group by`.

??? question "2. Add a test that the `localities` table has at least 3 rows"
    3, not 10,000 -- this sandbox's whole fixture is six made-up towns. A singular
    test, `tests/assert_localities_not_tiny.sql`:

    ```sql
    select 'localities is suspiciously small' as failure
    from {{ ref('localities') }}
    having count(*) < 3
    ```

    Returns a row only when the table is too small. In the real project a check
    like this exists as a data contract (`contracts/bus-marts.odcs.yaml`), sized
    for the real ~25,000 localities, not this sandbox's 6 -- a broken build there
    cannot publish an empty table either.

??? question "3. Predict before you run: what does `dbt ls --resource-type model --select +line_punctuality` show?"
    `line_punctuality`, `int_journey_delays`, `int_journeys_shaped_and_directed`,
    `int_pings_segmented_to_journeys` and `stg_bods__vehicle_pings`: the mart and
    everything upstream of it, back to the staging model. Then check with `dbt ls`.

You have finished the ride. When you are done, throw the sandbox away (it is only
a folder) or rebuild it with `ops/dbt_sandbox.sh`. There is a
[cheat sheet](cheatsheet.md) for the commands.
