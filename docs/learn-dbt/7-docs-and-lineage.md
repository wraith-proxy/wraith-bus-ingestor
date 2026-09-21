# Stop 7: The map of everything

You have been writing descriptions in `.yml` files, in comments and in the model
headers. dbt can turn all of it into a **website** for your project, with search,
descriptions, and an interactive map of the family tree.

## Make it

```bash
dbt docs generate --static
```

It takes a few seconds. It writes one self-contained file:

```text
target/static_index.html
```

Open that file in a browser (copy it to your own computer if you are working on a
remote machine, then double-click it). Everything runs inside that one file.

## What to look for

- **The left panel** lists every model, source and test, grouped by folder.
- **Click a model**, for example `localities`. You get its description, every
  column with its description and tests, the SQL you wrote, and the compiled SQL.
- **The graph button** (bottom right) draws the family tree from stop 3. Click
  `eta_models` and see everything it depends on lit up.
- **The search box** finds any model or column.

Those column descriptions came from the `.yml` files you have been reading, such
as `_marts__models.yml`. Documentation that lives next to the code stays true,
because whoever changes the model sees the words right beside it.

## The real one

The project publishes its own copy on this site: the **dbt Docs** tab at the top of
the page. It is generated from the *real* warehouse, so you can see the true row
counts and column types there.

## Tinker

1. Add a `description:` to your `big_localities.yml`. Regenerate and find it.
2. In the graph, click `big_localities`. Where does it sit relative to
   `localities`?

??? question "Why is the docs page a single HTML file?"
    `--static` puts the project's manifest and catalog inside one file, so you can
    email it, host it anywhere, or commit it, with no server needed.

!!! tip "Opinion"
    Describe every mart and every key column in the YAML. Undescribed columns are
    where wrong assumptions come from. If a description is hard to write, the model
    is unclear.

Next: [Landing, how the real nightly build uses all of this](landing.md).
