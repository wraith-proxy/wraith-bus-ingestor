# Stop 1: Take off

Before dbt can do anything you need somewhere safe to fly. We make a **sandbox**:
a private copy of the project, with its own database file and a small slice of
the real data (three files of bus positions, plus the national list of bus
stops).

## Build it

From the root of the repo:

```bash
ops/dbt_sandbox.sh
```

You should see something like:

```text
wrote /home/you/dbt-sandbox/data/bus_positions_sandbox.parquet: 275 pings, anchored on 2026-09-21
sandbox created at /home/you/dbt-sandbox
  7.9M total, synthetic data only (see the header of this script)

next:   source /home/you/dbt-sandbox/sandbox.env
```

The data is **synthetic**, not a slice of the real feed: six fictional towns of
bus stops and a handful of made-up bus journeys between them, small enough to
read in full and regenerated fresh every time you run this script. Real bus
positions live in QuestDB and are deliberately not committed to this repo --
there is nothing for a fresh clone to copy, which is exactly why the sandbox
generates its own.

Now step into it:

```bash
source ~/dbt-sandbox/sandbox.env
```

That one line does two things: it puts the right `dbt` on your path, and it tells
dbt to use the settings in the sandbox. You are now standing inside
`~/dbt-sandbox`. Check dbt is alive:

```bash
dbt --version
```

You will see a version number and the word `duckdb`. **DuckDB** is the database
this project uses: a whole database in a single file, no server to set up.

!!! note "Anything goes wrong, or you have made a mess?"
    Run `ops/dbt_sandbox.sh` again. It deletes the sandbox and makes a fresh one.

## What is in the box?

```bash
ls
```

| Thing | What it is |
|---|---|
| `models/` | Your SQL files. **This is where most of the action is.** |
| `macros/` | Reusable snippets of SQL |
| `tests/` | Checks that must pass |
| `data/` | The small, synthetic slice of data the sandbox generated |
| `dbt_project.yml` | The project's settings |
| `profiles.yml` | Where the database file lives and how much memory it may use |

## Your first run

Two of the project's models are tiny. Ask dbt to build just those:

```bash
dbt run --select stg_naptan__stops localities
```

Read what it prints. The important lines look like this:

```text
1 of 2 OK created sql view model main.stg_naptan__stops ...... [OK in 0.13s]
2 of 2 OK created sql table model main.localities ............ [OK in 0.17s]
Done. PASS=2 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=2
```

You just turned two SQL files into a *view* and a *table* in a database. Now look
at what you made:

```bash
dbt show --select localities --limit 5
```

```text
| locality_norm | locality   |     lat |     lon | n_stops |
| ------------- | ---------- | ------- | ------- | ------- |
| SANDOPOLIS    | Sandopolis | 53.422… | -1.749… |      14 |
| MAPLEFORD     | Mapleford  | 53.304… | -1.996… |       9 |
| DUCKVILLE     | Duckville  | 53.258… | -2.621… |      22 |
| BRIDGETON     | Bridgeton  | 53.285… | -1.743… |       5 |
| OLD MILL      | Old Mill   | 53.300… | -2.121… |       3 |
```

Each row is a made-up place with a made-up handful of bus stops, a centre point
and how many stops it has. Real `localities` rows look the same shape; there are
just six places here instead of thousands. Row order isn't controlled by
anything in the model, so the order you see may not match.

`dbt show` is your window into the database. It also runs any SQL you like:

```bash
dbt show --inline "select locality, n_stops from {{ ref('localities') }} order by n_stops desc" --limit 5
```

```text
| locality   | n_stops |
| ---------- | ------- |
| Duckville  |      22 |
| Sandopolis |      14 |
| Mapleford  |       9 |
| Bridgeton  |       5 |
| Old Mill   |       3 |
```

(Ignore the `{{ ref(...) }}` bit for now. It is stop 3.)

!!! tip "Opinion"
    Do **everything** in the sandbox until you know what you are doing. There is no
    prize for using the real thing early, and a real chance of breaking tonight's
    build.

## Check yourself

??? question "What does `--select` do?"
    It tells dbt *which* models to run. Without it, `dbt run` builds all of them.
    You will use `--select` constantly.

??? question "The sandbox has its own database file. Why does that matter?"
    Because the real nightly build uses a different file. Anything you do in the
    sandbox is completely separate from it, so you cannot break tonight's build.

Next: [Stop 2, A model is just a SELECT](2-a-model-is-a-select.md).
