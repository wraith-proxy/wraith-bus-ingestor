# Cheat sheet

Everything below was run in the sandbox. Run them from inside it
(`source ~/dbt-sandbox/sandbox.env` first).

## Commands

| Command | What it does |
|---|---|
| `dbt run` | Build every model |
| `dbt run --select localities` | Build one model |
| `dbt run --select +localities` | Build it **and everything it depends on** |
| `dbt run --select localities+` | Build it **and everything built from it** |
| `dbt run --select a b` | Build two named models |
| `dbt run --full-refresh` | Rebuild incremental models from scratch |
| `dbt test` | Run every test (build the models first) |
| `dbt test --select localities` | Test one model |
| `dbt build` | Run then test each model, in dependency order |
| `dbt show --select localities --limit 5` | Look at a model's rows |
| `dbt show --inline "select ..."` | Run any SQL and see the result |
| `dbt compile --select localities` | See the SQL after Jinja, without running it |
| `dbt ls --resource-type model` | List the models |
| `dbt source freshness` | Is the incoming data fresh enough? |
| `dbt docs generate --static` | Make the documentation site (`target/static_index.html`) |
| `ops/dbt_sandbox.sh` | Throw the sandbox away and make a fresh one |

## Brackets

| You write | Means |
|---|---|
| `{{ ref('model') }}` | The table for another model (and a dependency) |
| `{{ source('naptan', 'stops') }}` | Raw data from outside dbt |
| `{{ var('extract_days') }}` | A setting from `dbt_project.yml` |
| `{{ config(materialized='view') }}` | Settings for this one model |
| `{% if is_incremental() %} ... {% endif %}` | "Only on later runs" |
| `{{ this }}` | The table this model is building |
| `{# ... #}` | A comment |

## Words

| Word | Means |
|---|---|
| **Model** | A `.sql` file with one `SELECT`. It becomes a table or view. |
| **Source** | Data that arrives from outside dbt (here: files on disk). |
| **ref** | How one model uses another. Also how dbt learns the order. |
| **DAG** | The dependency map: arrows that never loop. |
| **Materialization** | How a model is stored: view, table or incremental. |
| **Incremental** | A table that only recomputes its newest part. |
| **Test** | A check. A query that must return no rows. |
| **Macro** | A function that writes SQL. |
| **Jinja** | The `{{ }}` templating dbt runs before the database sees anything. |
| **Staging / intermediate / marts** | The three layers: clean, think, serve. |
| **Sandbox** | Your private copy of the project. Do everything here. |

## When it goes wrong

| You see | It means |
|---|---|
| `Binder Error: Referenced column ... not found` | A column typo in your SQL. It suggests the right name. |
| `depends on a node named ... which was not found` | A `ref()` to a model that does not exist. Check spelling. |
| `FAIL 9609` on a test | The data is wrong: 9609 bad rows. Open the compiled query. |
| `ERROR` on a test | dbt could not run the check, usually because the model is not built yet. |
| Everything is a mess | `ops/dbt_sandbox.sh`, and start again. |
