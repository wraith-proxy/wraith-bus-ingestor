# Contracts

<a id="policy"></a>

## policy

Expand/contract gate for ODCS contract changes.

check_change(base, head) compares two contract files and returns the policy
violations of going from base to head. sunset/backfill are customProperties
entries ({property, value}) on a schema property.

This module is the pure policy check behind the contract change gate: it reads
two contract files (and optionally a directory of compiled SQL) and reports
violations. It must never open a live database or warehouse, and never modify
any contract file.

<a id="policy.Violation"></a>

### Violation

```python
@dataclass(frozen=True)
class Violation()
```

One policy violation found by check_change.

**Attributes**:

- `code` - Stable machine-readable violation code, for example
  "removal-without-sunset".
- `message` - Human-readable description naming the table.column at fault.

<a id="policy.removed_contracts"></a>

### removed\_contracts

```python
def removed_contracts(base_listing, root: Path) -> list[str]
```

Contract files on base (paths like contracts/x.odcs.yaml) missing under root.

**Arguments**:

- `base_listing` - Paths of files present on the base ref; only those ending
  in ".odcs.yaml" are considered.
- `root` - Directory the paths are resolved against.
  

**Returns**:

  The contract paths from base_listing that do not exist under root.

<a id="policy.check_change"></a>

### check\_change

```python
def check_change(base: Path,
                 head: Path,
                 *,
                 today: date | None = None,
                 compiled_sql_dir: Path | None = None) -> list[Violation]
```

Compare two contract files and return the policy violations of going from base to head.

**Arguments**:

- `base` - Contract file as it is on the base ref.
- `head` - Contract file as it is on the head ref.
- `today` - Date used to judge sunsets; defaults to date.today().
- `compiled_sql_dir` - Optional directory of compiled SQL files. When given, a
  removed column that any file still reads is a violation.
  

**Returns**:

  The violations found; an empty list means the change is allowed.
  

**Raises**:

- `FileNotFoundError` - If base or head does not exist.
- `yaml.YAMLError` - If either file is not valid YAML.

<a id="policy.main"></a>

### main

```python
def main(argv)
```

Command-line entry point for the contract change gate.

Usage is either `base head [compiled_sql_dir]` to run check_change and print
one "code: message" line per violation, or `--deleted listing [root]` to report
contract files present on origin/main but missing under root.

**Arguments**:

- `argv` - Command-line arguments, without the program name.
  

**Returns**:

  Process exit code: 1 if any violation or deleted contract was reported,
  otherwise 0.

<a id="validate"></a>

## validate

Check live data against its ODCS contracts, and the ontology against both.

A data contract nobody executes is a document. This runs the obligations:
every declared object and property must exist with a compatible type, every
required property must be non-null, every declared key must be unique, and
every quality rule must hold.

    contracts/validate.py                      # both contracts + the ontology
    contracts/validate.py --contract contracts/bus-marts.odcs.yaml
    contracts/validate.py --marts /tmp/candidate.duckdb   # gate a rebuild
    contracts/validate.py --gtfs /tmp/candidate_gtfs.duckdb   # gate a GTFS refresh

Exit code 0 if every obligation holds, 1 otherwise, so it can gate a build.
Passing --marts selects gate mode: the marts file must open and at least one
obligation must actually be checked, or the run fails ("nothing checked"). A bare
run still degrades to a skip when an engine is unreachable (CI has none).
Rules whose severity is 'info', and `text` rules, are reported but never fail.

This module is a read-only checker. It must never write to a warehouse or to
QuestDB (DuckDB files are opened read-only, QuestDB is only queried), and it
never edits a contract; `--generated` regenerates into a temporary directory.

<a id="validate.Result"></a>

### Result

```python
class Result()
```

Running tally of passes, failures, warnings and skips, printed as they happen.

**Attributes**:

- `failures` - "what: detail" lines for unmet error-severity obligations.
- `warnings` - "what: detail" lines for failed info-severity rules.
- `passes` - Count of obligations that held.
- `skips` - Count of obligations that could not be judged.

<a id="validate.Result.__init__"></a>

#### \_\_init\_\_

```python
def __init__()
```

Start with nothing recorded.

<a id="validate.Result.ok"></a>

#### ok

```python
def ok(what)
```

Record and print a passing obligation.

**Arguments**:

- `what` - Description of the obligation that held.

<a id="validate.Result.fail"></a>

#### fail

```python
def fail(what, detail, severity="error")
```

Record and print an unmet obligation.

**Arguments**:

- `what` - Description of the obligation.
- `detail` - Why it was not met.
- `severity` - "info" records a warning that never fails the run; any
  other value records a failure.

<a id="validate.Result.skip"></a>

#### skip

```python
def skip(what, why)
```

Record and print an obligation that could not be judged.

**Arguments**:

- `what` - Description of the obligation.
- `why` - Reason it was skipped.

<a id="validate.DuckDBEngine"></a>

### DuckDBEngine

```python
class DuckDBEngine()
```

Read-only DuckDB file engine used to check the marts and served GTFS contracts.

<a id="validate.DuckDBEngine.__init__"></a>

#### \_\_init\_\_

```python
def __init__(path)
```

Open a DuckDB file read-only.

**Arguments**:

- `path` - Path to the DuckDB database file.
  

**Raises**:

- `RuntimeError` - If path does not exist or is a directory.

<a id="validate.DuckDBEngine.columns"></a>

#### columns

```python
def columns(table)
```

Look up a table's columns.

**Arguments**:

- `table` - Table name.
  

**Returns**:

  {column name: upper-cased data type}; empty if the table is absent.

<a id="validate.DuckDBEngine.scalar"></a>

#### scalar

```python
def scalar(sql)
```

Run a query and return its first value.

**Arguments**:

- `sql` - A query returning at least one row and column.
  

**Returns**:

  The first column of the first row.

<a id="validate.DuckDBEngine.quote"></a>

#### quote

```python
def quote(ident)
```

Quote an identifier for this engine.

**Arguments**:

- `ident` - Column or table name.
  

**Returns**:

  The name double-quoted, with embedded quotes doubled.

<a id="validate.DuckDBEngine.scope"></a>

#### scope

```python
def scope(table)
```

Return the SQL fragment that stands for the table in a check.

**Arguments**:

- `table` - Table name.
  

**Returns**:

  The quoted table name; DuckDB checks cover the whole table.

<a id="validate.DuckDBEngine.close"></a>

#### close

```python
def close()
```

Close the connection.

<a id="validate.QuestDBEngine"></a>

### QuestDBEngine

```python
class QuestDBEngine()
```

Engine that checks QuestDB over a recent window only.

QuestDB holds ~1e9 rows of append-only history, so validating the whole
table is neither affordable nor meaningful -- the contract describes what the
ingestor is landing NOW, and historical rows predate several of its clauses.
Every check is therefore scoped to a recent window by substituting a bounded
subquery wherever a rule names the object.

Only ever queries QuestDB over HTTP (/exec); it never writes.

<a id="validate.QuestDBEngine.__init__"></a>

#### \_\_init\_\_

```python
def __init__(url, window_minutes=5)
```

Point the engine at a QuestDB HTTP endpoint.

**Arguments**:

- `url` - QuestDB HTTP base URL.
- `window_minutes` - Recent window, in minutes, every check is scoped to.

<a id="validate.QuestDBEngine.columns"></a>

#### columns

```python
def columns(table)
```

Look up a table's columns.

**Arguments**:

- `table` - Table name.
  

**Returns**:

  {column name: upper-cased QuestDB type}.
  

**Raises**:

- `RuntimeError` - If QuestDB reports an error, for example a missing table.

<a id="validate.QuestDBEngine.scalar"></a>

#### scalar

```python
def scalar(sql)
```

Run a query and return its first value.

**Arguments**:

- `sql` - A query returning at least one row and column.
  

**Returns**:

  The first column of the first row.
  

**Raises**:

- `RuntimeError` - If QuestDB reports an error.

<a id="validate.QuestDBEngine.quote"></a>

#### quote

```python
def quote(ident)
```

Return an identifier for this engine.

**Arguments**:

- `ident` - Column or table name.
  

**Returns**:

  The name unchanged; QuestDB identifiers are not quoted here.

<a id="validate.QuestDBEngine.scope"></a>

#### scope

```python
def scope(table)
```

Return the SQL fragment that stands for the table in a check.

**Arguments**:

- `table` - Table name.
  

**Returns**:

  A subquery restricting the table to the recent window.

<a id="validate.QuestDBEngine.close"></a>

#### close

```python
def close()
```

Nothing to release; QuestDB is queried statelessly over HTTP.

<a id="validate.types_compatible"></a>

### types\_compatible

```python
def types_compatible(declared, actual)
```

Judge whether a declared physical type matches the engine's actual type.

**Arguments**:

- `declared` - Physical type declared in the contract, for example "VARCHAR(18)".
- `actual` - Type reported by the engine.
  

**Returns**:

  True if they are equal, equal after dropping a length suffix, or listed
  as aliases in TYPE_ALIASES.

<a id="validate.check_quality"></a>

### check\_quality

```python
def check_quality(engine, res, rule, table, prop=None)
```

Run one ODCS quality rule. Returns nothing; records into res.

**Arguments**:

- `engine` - The engine to evaluate against.
- `res` - Result that receives the pass, failure or skip.
- `rule` - The ODCS quality rule dict.
- `table` - Table (object) the rule belongs to.
- `prop` - Property (column) the rule belongs to, or None for an object rule.

<a id="validate.validate_contract"></a>

### validate\_contract

```python
def validate_contract(path, engines, res)
```

Check one contract's objects, properties and quality rules against live data.

**Arguments**:

- `path` - Path to an ODCS contract file.
- `engines` - Available engines by server name or type ("duckdb", "gtfs-served",
  "custom"); the contract's server picks one, falling back to "duckdb".
- `res` - Result that receives every pass, failure and skip.
  

**Returns**:

  The parsed contract.

<a id="validate.validate_ontology"></a>

### validate\_ontology

```python
def validate_ontology(path, contracts, res)
```

Validate the ontology against the contracts.

Every ontology binding must resolve to a real contract property, and any
enum stated in both places must agree. This is what stops the semantic layer
drifting away from the data underneath it.

**Arguments**:

- `path` - Path to the ontology file.
- `contracts` - Parsed contracts validated this run, by file basename.
- `res` - Result that receives every pass, failure and skip.
  

**Returns**:

  The parsed ontology.

<a id="validate.validate_ossie_model"></a>

### validate\_ossie\_model

```python
def validate_ossie_model(path, onto, contracts, res)
```

Validate the Ossie model against the ontology and the contracts.

The Ossie model (objects, links, metrics) must agree with the Foundry-style ontology,
which stays the source of bindings, and through it with the contracts: every dataset
source names a real contract object, every field reads a real column and the column the
Foundry binding names, relationships and metrics use only declared datasets and fields.

**Arguments**:

- `path` - Path to the Ossie model file.
- `onto` - The parsed Foundry-style ontology, from validate_ontology.
- `contracts` - Parsed contracts validated this run, by file basename.
- `res` - Result that receives every pass, failure and skip.

<a id="validate.check_generated"></a>

### check\_generated

```python
def check_generated(root, generate=None)
```

Problems (list of str) where committed generated files differ from a fresh regeneration.

Only the modelgen version in the header line is tolerated; any other difference,
a missing file or a stale generated file is a problem. `generate(workdir)` is injectable.

**Arguments**:

- `root` - Repository root holding .modelgen.yml and the committed generated files.
- `generate` - Callable taking a working directory and regenerating into it;
  defaults to running modelgen there.
  

**Returns**:

  One message per problem; empty when everything is in sync.

<a id="validate.main"></a>

### main

```python
def main()
```

Run the validator from the command line.

Reads sys.argv (see the module docstring for the options), connects to the
engines that are available, validates each contract, then the ontology and
Ossie model, and prints a summary. With --generated it only checks generated
dbt files for drift.

**Returns**:

  Process exit code: 0 if every obligation holds, otherwise 1.

