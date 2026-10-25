# Stop 5: view, table, incremental

A model is just a `SELECT`. But there are different ways dbt can turn that
`SELECT` into something in the database. That choice is called the
**materialization**, and getting it right is why this project's nightly build went
from about an hour to about a quarter of an hour.

## The three you need

| Materialization | What dbt builds | Good when |
|---|---|---|
| **view** | A saved query. No data stored; it runs every time you read it. | The query is cheap, or you always want fresh data. |
| **table** | A real table, rebuilt from scratch each run. | The result is read a lot and is worth storing. |
| **incremental** | A real table that is **not** rebuilt: each run only adds or replaces the *new* part. | The input is huge and most of it never changes. |

Think of a view as a *recipe*, a table as a *cooked meal*, and incremental as a
*meal you only top up*.

## Where this project chooses

Open `dbt_project.yml` (trimmed here):

```yaml
models:
  wraith_bus_ingestor:
    +materialized: view
    staging:
      +materialized: view
    intermediate:
      +materialized: view
    marts:
      +materialized: table
```

- **Staging is a view.** It only renames and casts columns, which costs nothing,
  and a view always reflects the files currently on disk.
- **Marts are tables.** The API reads them all day, so they must be real, stored
  tables.
- **One intermediate model is special.** It sets its own materialization at the top
  of its file.

## See the difference

Ask the database what it holds:

```bash
dbt show --inline "select table_name, table_type from information_schema.tables where table_schema = 'main' order by 1" --limit 20
```

You will see `stg_...` marked `VIEW` and the marts marked `BASE TABLE`.

Now change your own model. Put this line at the very top of
`models/marts/big_localities.sql`:

```sql
{{ config(materialized='view') }}
```

Run it and ask again:

```bash
dbt run --select big_localities
dbt show --inline "select table_name, table_type from information_schema.tables where table_name = 'big_localities'"
```

It is now a `VIEW`. Delete the line, run again, and it is a table again. A
config line in the file wins over the folder default.

## The star of the show: incremental

`int_pings_segmented_to_journeys` takes a bus's endless stream of GPS pings and cuts
it into individual journeys. It uses window functions over **millions of rows**.
In the real build, doing that from scratch every night took **around an hour**,
which is far too long, and most of the work was redoing yesterday's answer.

So it is incremental. The top of the file says so:

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='ping_local_date',
    ...
) }}
```

and further down there is the clever bit:

```sql
    {% if is_incremental() %}
    where ping_local_date >= (select max(ping_local_date) from {{ this }}) - interval '2 days'
    {% endif %}
```

Read it slowly:

- `is_incremental()` is true on every run *except the first*, when the table
  already exists.
- `{{ this }}` means "the table I am building", so the model can look at its own
  previous output.
- So on a normal night it reads **only the last couple of days**, throws away those
  days from the old table (`delete`), and puts the fresh version in (`insert`).
  Older days are never touched.

The first run does the whole lot. Every later run is quick. The days the table holds:

```bash
dbt show --inline "select ping_local_date, count(*) as pings from {{ ref('int_pings_segmented_to_journeys') }} group by 1 order by 1"
```

The sandbox only has a couple of days; the real warehouse keeps eight.

!!! info "The price of speed"
    An incremental model can *drift*: if the logic changes, the old rows still
    hold the old answer. The escape hatch is `dbt run --full-refresh`, which
    throws the table away and rebuilds it from scratch. Not always: a model can
    set `full_refresh` in its own config, and that wins over the flag. This
    project's two history tables set it to false (except during a replay),
    because they keep days after the raw data behind them is deleted. The real
    nightly uses this only when it has to (for example after a change to how
    journeys are keyed).

## The numbers, for real

| Nightly build | About |
|---|---|
| Before (rebuilding everything, and often failing) | **66 minutes** |
| After fixing the incremental window | 22 minutes |
| After deleting 89 unneeded models | **15 minutes** (most of it fetching data, not dbt) |

The whole `dbt run` is now about two minutes. The full story is in
[ADR-0009](../adr/0009-fast-nightly-build.md).

!!! tip "Opinion"
    - **Views for staging. Tables for marts.** Set it once, in `dbt_project.yml`.
    - **Incremental is a last resort.** Use it only when a full rebuild is too slow,
      because it is harder to get right and it can drift. This project has three:
      segmentation, which earned it on speed, and two small history tables, which
      earned it by keeping a daily summary after the raw pings are deleted.
    - **Measure first.** The build got fast because someone timed every model and
      found one of them was 85% of the cost.

## Check yourself

??? question "You change the logic of an incremental model. Why might the table now be wrong?"
    Because only the last couple of days get recomputed with the *new* logic. Older
    rows keep whatever the *old* logic produced. Fix: `--full-refresh`.

??? question "Why is `localities` a table rather than a view?"
    A view would re-read the NaPTAN CSV and re-do the aggregation every time the API
    asked. A table does that once per night.

Next: [Stop 6, Jinja and macros](6-jinja-and-macros.md).
