# Stop 6: Jinja and macros

You have been seeing `{{ ... }}` all trip. That is **Jinja**, a tiny templating
language. dbt runs your file through Jinja first, *then* sends the result to the
database. It is the one genuinely new thing to learn, and it is small.

## Two kinds of brackets

| You write | It means |
|---|---|
| `{{ something }}` | **Print** something into the SQL. |
| `{% something %}` | **Do** something (an `if`, a `for`, a definition). It prints nothing itself. |
| `{# something #}` | A comment. It vanishes before the database sees it. |

You have already used all three. `{{ ref('x') }}` prints a table name.
`{% if is_incremental() %}` chooses whether some SQL is included at all. The big
`{# ... #}` blocks at the top of the real models are long comments.

## Variables

Project-wide settings live in `dbt_project.yml`:

```yaml
vars:
  extract_days: 4
```

and you read them with `var()`:

```bash
dbt show --inline "select {{ var('extract_days') }} as days"
```

```text
| days |
| ---- |
|    4 |
```

## Macros: a function for SQL

If you find yourself writing the same chunk of SQL twice, you want a **macro**.
Look at `macros/locality_norm.sql`:

```sql
{% macro locality_norm(expr) -%}
trim(regexp_replace(
  upper(regexp_replace({{ expr }}, '[^A-Za-z0-9 ]', ' ', 'g')),
  ' +', ' ', 'g'
))
{%- endmacro %}
```

It cleans up a place name so different spellings match: upper-case, punctuation
turned to spaces, extra spaces squashed. The bus stop data spells one town as
"Ashton-under-Lyne", "ASHTON UNDER LYNE" or "Ashton under Lyne", and a user typing
in the search box could type any of them. The macro turns them all into
`ASHTON UNDER LYNE`.

Call it like a function:

```bash
dbt show --inline "select {{ locality_norm(\"'St. Helens'\") }} as key, {{ locality_norm(\"'ashton-under-Lyne'\") }} as key2"
```

```text
| key       | key2              |
| --------- | ----------------- |
| ST HELENS | ASHTON UNDER LYNE |
```

The macro is used in the staging model *and* the search feature in the API applies
the same rule, so the two sides always agree. That is the real reason macros
exist: **one definition, used everywhere**, so they cannot drift apart.

## Write your own

Let us label each place as a village, town or city by how many stops it has.
Create `macros/size_label.sql`:

```bash
cat > macros/size_label.sql <<'SQL'
{% macro size_label(n) -%}
case
    when {{ n }} >= 15 then 'city'
    when {{ n }} >= 6  then 'town'
    else 'village'
end
{%- endmacro %}
SQL
```

Use it:

```bash
dbt show --inline "select locality, n_stops, {{ size_label('n_stops') }} as size from {{ ref('localities') }} order by n_stops desc" --limit 6
```

```text
| locality   | n_stops | size    |
| ---------- | ------- | ------- |
| Duckville  |      22 | city    |
| Sandopolis |      14 | town    |
| Mapleford  |       9 | town    |
| Bridgeton  |       5 | village |
| Old Mill   |       3 | village |
| Nowhereton |       1 | village |
```

Now look at what dbt *really* sent to the database:

```bash
dbt compile --inline "select {{ size_label('n_stops') }} from {{ ref('localities') }}"
```

The macro has been replaced with its full `case` expression. Jinja is only ever
text substitution; the database never hears about it.

## Tinker

1. Change the thresholds. Which places change label?
2. Make a model `models/marts/place_sizes.sql` that uses `size_label` and run it.
3. Add a second argument to the macro for the "city" threshold, with a default:
   `{% macro size_label(n, city=15) -%}`.

??? question "Where would you put a rule that three different models all need?"
    In a macro. Write it once in `macros/`, call it from each model.

??? question "Why does the macro get the *name* of a column (`'n_stops'`) rather than a value?"
    Because a macro runs *before* the database runs anything. There are no values
    yet, only SQL text. The macro pastes `n_stops` into the query, and the database
    then evaluates it for every row.

!!! tip "Opinion"
    - Use Jinja to remove **repetition**, not to be clever. If a reader cannot tell
      what the SQL will be, you have gone too far.
    - Reach for a macro when the *same rule* lives in two places, like the place-name
      cleaner. Not for a one-off.
    - Prefer plain SQL first. A macro nobody understands is worse than three copies.

Next: [Stop 7, The map of everything](7-docs-and-lineage.md).
