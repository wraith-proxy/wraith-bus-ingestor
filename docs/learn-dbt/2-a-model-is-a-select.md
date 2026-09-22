# Stop 2: A model is just a SELECT

Here is the secret that makes dbt easy: **a "model" is a `.sql` file containing
one `SELECT`.** That is it. The file name becomes the table name.

## Read a real one

Open `models/marts/localities.sql`. (It powers the place search box on the bus
map.) Ignoring the comments, it is this:

```sql
with stops as (

    select * from {{ ref('stg_naptan__stops') }}

),

by_locality as (

    select
        locality_norm,
        mode(locality)           as locality,
        avg(lat)                 as lat,
        avg(lon)                 as lon,
        cast(count(*) as bigint) as n_stops
    from stops
    group by locality_norm

)

select locality_norm, locality, lat, lon, n_stops
from by_locality
```

If you know SQL, you can read all of it. The only strange thing is
`{{ ref('stg_naptan__stops') }}`, which means "the output of that other model".
Stop 3 explains it.

Three things to notice:

1. **It is just a query.** No `CREATE TABLE`. You describe *what you want*; dbt
   writes the `CREATE TABLE ... AS` around it for you.
2. **It is written as small steps.** `with stops as (...), by_locality as (...)`
   are CTEs: a step called `stops`, then a step called `by_locality` that uses
   it. This is how the whole project is written, because small steps are easy to
   read.
3. **The comments explain the *why*.** For example, `mode(locality)` picks the
   most common spelling so "Ashton-under-Lyne" beats a stray "ASHTON UNDER
   LYNE". Read the comments in the real files; they are full of small stories.

## Run it, then look

```bash
dbt run --select localities
dbt show --select localities --limit 5
```

## Write your own

You are going to add a model that does not exist yet: *only the places with lots
of stops*. Make a new file:

```bash
cat > models/marts/big_localities.sql <<'SQL'
select locality, n_stops
from {{ ref('localities') }}
where n_stops >= 10
SQL
```

Build it and look:

```bash
dbt run --select big_localities
dbt show --select big_localities --limit 5
```

```text
1 of 1 OK created sql table model main.big_localities ...... [OK in 0.12s]
```

```text
| locality   | n_stops |
| ---------- | ------- |
| Duckville  |      22 |
| Sandopolis |      14 |
```

You have added a table to the project by creating one file. There is no
registration step; dbt finds every `.sql` file under `models/` by itself.

## Tinker

1. Change `10` to `20`. Now only one place should qualify -- which one, and why?
2. Add a second column, `lat`, to the `select`. Run it. Did dbt need to be told
   about the new column?
3. **Break it.** Change `n_stops` to `n_stopz` and run it.

??? question "What did the typo do?"
    dbt stops and prints the database's own error, pointing at the exact line:

    ```text
    Binder Error: Referenced column "n_stopz" not found in FROM clause!
    Candidate bindings: "n_stops"
    ```

    Notice it even suggests the right spelling. Fix it and run again. Reading
    these messages calmly is 80% of using dbt.

??? question "Why does `big_localities` end up as a *table* and not a view?"
    Because the project's settings say everything under `models/marts/` is a
    table. We meet that setting in [Stop 5](5-materializations.md).

!!! tip "Opinion"
    - One model, one file, one job. If you cannot describe a model in one sentence,
      split it.
    - Write small named CTEs. `select * from (select ... from (select ...))` is not
      a style.
    - Comment the **why**. Nobody needs a comment saying `avg(lat)` averages
      latitude.

Next: [Stop 3, ref(): how models find each other](3-ref-and-the-graph.md).
