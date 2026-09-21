# Stop 4: Tests, models that check themselves

Building a table is easy. Building a table that is **right** is the hard part.
dbt lets you write down what "right" looks like and checks it every time.

## Why bother? A true story

The app finds a bus's expected arrival time by walking down a list of models until
it finds one that fits. The very last entry is a "global fallback" that matches
everything. If that row is ever missing, **every bus on the map silently loses its
ETA**, and nothing crashes; the numbers just quietly vanish. So the project has a
test for it: `tests/assert_eta_model_has_global_fallback.sql`. Open it.

## Kind 1: a test is a query that must return nothing

A **singular test** is a `.sql` file in `tests/`. It is a `SELECT` that returns
the rows that are *wrong*. If it returns zero rows, the test passes. If it returns
any rows, it fails.

Make one for your new model. We said "big" means at least 100 stops, so:

```bash
cat > tests/assert_big_localities_are_big.sql <<'SQL'
select * from {{ ref('big_localities') }}
where n_stops < 100
SQL
```

Set `big_localities.sql` back to `n_stops >= 100`, run it, and test it:

```bash
dbt run  --select big_localities
dbt test --select big_localities
```

```text
1 of 1 PASS assert_big_localities_are_big ...... [PASS in 0.04s]
```

### Now make it fail

Change `100` to `10` in the model, rebuild, and test again:

```bash
dbt run  --select big_localities
dbt test --select big_localities
```

```text
1 of 1 FAIL 9609 assert_big_localities_are_big ...... [FAIL 9609 in 0.04s]
  Got 9609 results, configured to fail if != 0
  compiled code at target/compiled/.../assert_big_localities_are_big.sql
```

(Your number will differ.) The test found places that are not "big". dbt even
tells you where the compiled query is, so you can run it and look at the bad rows.
Put the `100` back.

## Kind 2: the one-liners in YAML

Most checks are so common that dbt has them built in. You declare them next to the
model in a `.yml` file. Look at the real one, `models/marts/_marts__models.yml`:

```yaml
  - name: localities
    columns:
      - name: locality_norm
        description: Primary key. Uppercased, punctuation-stripped, ...
        tests: [not_null, unique]
```

`not_null` and `unique` are the two you will use most. Add the same to your own
model:

```bash
cat > models/marts/big_localities.yml <<'YML'
version: 2
models:
  - name: big_localities
    description: Places with at least 100 bus stops.
    columns:
      - name: locality
        tests: [not_null, unique]
YML
```

## build = run + test, in the right order

```bash
dbt build --select big_localities
```

`dbt build` runs each model and then straight away tests it, all in dependency
order. This is the command to reach for when you want to know "is my change
safe?".

The whole project has 88 tests (plus the ones you have added). To run them all,
first build everything, because a test cannot check a table that does not exist
yet (about 25 seconds), and then test (about 10):

```bash
dbt run
dbt test
```

!!! warning "Errors instead of passes?"
    If lots of tests say `ERROR` rather than `FAIL`, you have not built those
    models yet. A `FAIL` means "the data is wrong". An `ERROR` means "dbt could
    not even run the check". They are very different problems.

## Tinker

1. Write a test that fails if any locality has a latitude outside Great Britain
   (roughly 49.8 to 61.0). Hint: a query returning the bad rows.
2. Look at `tests/assert_temporal_buckets_are_local.sql`. What is it protecting?

??? question "Why does a passing test return *zero* rows instead of returning `true`?"
    Because a failing test is then also its own evidence. The rows it returns
    *are* the problems, so you can read exactly what went wrong.

??? question "Hint for the latitude test"
    ```sql
    select * from {{ ref('localities') }}
    where lat not between 49.8 and 61.0
    ```
    The real staging model already filters such rows out, so this test should
    pass. That is a useful thing to notice: a check can belong *after* the filter
    (proving it worked) as well as before it.

!!! tip "Opinion"
    - Test every key with `not_null` and `unique`. No excuses.
    - A failing test **blocks the publish**. "Warn for now" is how wrong numbers
      reach users. (Three tests here are `warn`; each has a comment explaining that
      it guards the incoming data, which we do not control.)
    - Test the thing that would hurt if it silently broke, not every column.

Next: [Stop 5, view, table, incremental](5-materializations.md).
