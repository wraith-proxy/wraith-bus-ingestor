# dbt scripts

<a id="extract"></a>

## extract

Pull a bounded window of bus_positions out of QuestDB into parquet for dbt.

QuestDB holds ~1e9 rows; dbt only needs enough to fit per-line models, so we
downsample to one row per bus per SAMPLE_MINS and keep the last EXTRACT_DAYS
UTC days. Each day is fetched once, as chunk files plus a .done marker, so a
nightly run only pulls the one day it does not have yet.

Run by the dbt build before the models. Environment: QUESTDB_HTTP,
EXTRACT_DAYS, EXTRACT_SAMPLE_MINS, EXTRACT_CHUNK_HOURS, QUESTDB_CONTAINER and
EXTRACT_RESTART_QUESTDB (see the constants below for defaults).

This module only reads from QuestDB and writes parquet files and .done markers
under dbt/data. It must never write to QuestDB, and it only deletes day files
that have slid out of the extract window.

<a id="extract.restart_questdb"></a>

### restart\_questdb

```python
def restart_questdb()
```

Restart the QuestDB container and wait for it to answer again.

**Raises**:

- `subprocess.CalledProcessError` - If docker restart fails.

<a id="extract.questdb_columns"></a>

### questdb\_columns

```python
def questdb_columns()
```

Ask QuestDB which bus_positions columns exist.

The schedule columns only appear once the patched ingestor has run, so
ask QuestDB what it actually has rather than guessing.

**Returns**:

  The set of column names of bus_positions.
  

**Raises**:

- `urllib.error.URLError` - If QuestDB is unreachable.

<a id="extract.build_select"></a>

### build\_select

```python
def build_select(available)
```

Build the SAMPLE BY select list and the read_csv column map.

Wanted columns not in available are reported and skipped.

**Arguments**:

- `available` - Column names QuestDB actually has, from questdb_columns.
  

**Returns**:

  A (select, columns) tuple: the QuestDB select list, and the DuckDB
  read_csv columns literal.

<a id="extract.fetch"></a>

### fetch

```python
def fetch(con, day, hour_start, hour_end, select, columns)
```

Fetch one chunk of a day from QuestDB into a parquet file.

**Arguments**:

- `con` - An open duckdb connection with httpfs loaded.
- `day` - The UTC date to fetch (a datetime.date).
- `hour_start` - First hour of the chunk, 0-23.
- `hour_end` - Hour the chunk ends before, 1-24.
- `select` - Select list from build_select.
- `columns` - read_csv columns literal from build_select.
  

**Returns**:

  A (path, row_count) tuple, or (None, 0) if the window had no rows and
  nothing was written.
  

**Raises**:

- `duckdb.Error` - If the QuestDB export or the parquet write fails.

<a id="extract.wal_applied"></a>

### wal\_applied

```python
def wal_applied()
```

Report whether QuestDB has applied every accepted bus_positions write.

(True, None) when QuestDB has applied every write it has accepted for
bus_positions, else (False, why). bus_positions is a WAL table: a commit
lands in the WAL first and becomes visible to queries only once the apply
job has written it to the table, and a suspended table stops applying
altogether. A day fetched while either was true can come back short, so
it is marked only if, once its chunks have landed, wal_tables() shows the
table not suspended and writerTxn caught up with sequencerTxn; otherwise
the next run fetches it again. Any failure to ask counts as not applied.

**Returns**:

  A (applied, reason) tuple: (True, None), or (False, why) when the WAL
  is lagging, the table is suspended, or the check itself failed.

<a id="extract.prune_outside_window"></a>

### prune\_outside\_window

```python
def prune_outside_window(first_day)
```

Delete extract files for days before the window.

The SSD keeps only the extract window; SATA is the archive. dbt's source
glob (bus_positions_*.parquet) reads every file in OUT, and nothing else
ever removed a day that had slid out of the window -- 2026-09-19's
nightly was reading 12 days (81.9M rows) against a 4GB memory_limit tuned
for 8. Only files whose date is before the window are touched, and a day's
.done marker goes with its chunks.

**Arguments**:

- `first_day` - First UTC date (a datetime.date) inside the window; files
  dated earlier are removed.
  

**Returns**:

  The sorted basenames of the files removed.

<a id="extract.main"></a>

### main

```python
def main()
```

Extract the window of bus_positions into parquet files under dbt/data.

Fetches each missing UTC day in chunks, marks complete days with a .done
file, then prunes days outside the window.

<a id="publish_operator_names"></a>

## publish\_operator\_names

Load noc_operator_names.csv into QuestDB for Grafana operator names.

This lets Grafana show real
operator names instead of raw four-letter NOC codes.

Unlike publish_run_results.py and publish_punctuality_summary.py, this is
not run by build.sh on every build -- the data isn't build-derived, it's a
static reference table, checked into git as noc_operator_names.csv (a
curated extract of the real National Operator Code database,
https://www.travelinedata.org.uk/noc/api/1.0/nocrecords.xml, covering the
operators actually seen in this feed's own bus_positions -- not the full
~3,300-operator UK database, which would be a lot of dead weight for a
panel that only ever shows the ten busiest). Run by hand, occasionally,
when a new operator's code shows up unmapped on the dashboard:

    dbt/.venv/bin/python publish_operator_names.py    (or the repo's own
                                                         .venv, same as
                                                         every other
                                                         publish_*.py)

QuestDB rather than a dbt seed: dbt seeds load into the duckdb warehouse,
which Grafana cannot reach (no wire protocol) -- same reasoning as every
other publish_*.py script in this directory.

Environment:
    QUESTDB_HTTP: QuestDB HTTP base URL (default http://localhost:9001).

Exit codes:
    0 on success, 1 when the publish fails.

<a id="publish_operator_names.esc_tag"></a>

### esc\_tag

```python
def esc_tag(val)
```

Escape a value for use as an ILP tag.

**Arguments**:

- `val` - Value to escape; coerced to str.
  

**Returns**:

  The string with spaces, commas and equals signs replaced by underscores.

<a id="publish_operator_names.esc_str"></a>

### esc\_str

```python
def esc_str(val)
```

Escape a value for use inside a quoted ILP string field.

**Arguments**:

- `val` - Value to escape; coerced to str.
  

**Returns**:

  The string with backslashes and double quotes escaped.

<a id="publish_operator_names.build_lines"></a>

### build\_lines

```python
def build_lines(csv_path)
```

Build ILP lines from the operator names CSV, skipping incomplete rows.

**Arguments**:

- `csv_path` - Path to a CSV with operator_code and operator_name columns.
  

**Returns**:

  A list of ILP lines, one per row that has both a code and a name.

<a id="publish_operator_names.publish"></a>

### publish

```python
def publish(csv_path)
```

POST the operator names to QuestDB over ILP/HTTP.

**Arguments**:

- `csv_path` - Path to the operator names CSV.
  

**Returns**:

  A tuple of (line count, HTTP status).
  

**Raises**:

- `urllib.error.URLError` - QuestDB is unreachable or rejects the request.

<a id="publish_operator_names.main"></a>

### main

```python
def main()
```

Publish the CSV named by argv[1], or the default CSV.

**Raises**:

- `SystemExit` - Exit code 1 when the publish fails.

<a id="publish_punctuality_summary"></a>

## publish\_punctuality\_summary

Publish per-line punctuality and ETA model-fit summaries into QuestDB.

Run by build.sh right after the marts are published (mv'd to
wraith_bus_ingestor.duckdb), so this reads the same file the API now
serves -- one authoritative snapshot, not the scratch build warehouse.

QuestDB rather than the duckdb marts themselves, same reason as
publish_run_results.py: duckdb has no wire protocol Grafana can reach.
line_punctuality and eta_models are per (line, direction, day_type,
hour_band) grain -- too fine to plot directly -- so this collapses each
to one row per line (punctuality) or one row overall (model fit) before
writing. That collapse is an approximation, not a re-derivation from raw
per-journey delays (BODS/this pipeline never stores an actual per-journey
delay, only the grain-level median/p90 dbt already computed), and the
per-line comment below says exactly what it approximates.

Usage: publish_punctuality_summary.py [marts_path]. The marts file defaults to
wraith_bus_ingestor.duckdb beside this script. Environment: QUESTDB_HTTP
(default http://localhost:9001). Exit code: always 0.

This module must never write to the duckdb marts (it opens them read-only) and
must never fail a build: every error degrades to a warning.

<a id="publish_punctuality_summary.esc_tag"></a>

### esc\_tag

```python
def esc_tag(val)
```

Escape a value for use as an ILP tag.

**Arguments**:

- `val` - The tag value; converted with str().
  

**Returns**:

  The value with spaces, commas and equals signs replaced by underscores.

<a id="publish_punctuality_summary.build_lines"></a>

### build\_lines

```python
def build_lines(con)
```

Turn the two duckdb marts into ILP lines.

Kept separate from the
connection/POST so it can be exercised against any duckdb connection,
live or a fixture, without a real QuestDB.

**Arguments**:

- `con` - An open duckdb connection with the line_punctuality and eta_models
  marts.
  

**Returns**:

  ILP lines: one line_punctuality_summary line per line, then one
  eta_model_summary line.

<a id="publish_punctuality_summary.publish"></a>

### publish

```python
def publish(marts_path)
```

Read the marts read-only and POST the summaries to QuestDB.

**Arguments**:

- `marts_path` - Path to the published duckdb marts file.
  

**Returns**:

  A (line_count, http_status) tuple.
  

**Raises**:

- `duckdb.Error` - If the marts file cannot be opened or queried.
- `urllib.error.URLError` - If QuestDB is unreachable or rejects the write.

<a id="publish_punctuality_summary.main"></a>

### main

```python
def main()
```

Publish the summaries for argv[1], or the default marts file.

Always exits 0; a failure is reported as a warning on stderr.

<a id="publish_run_results"></a>

## publish\_run\_results

Publish dbt run results into QuestDB so Grafana has build health to plot.

Run by dbt/build.sh after every dbt invocation, pass or fail. dbt writes
target/run_results.json on every run, so that file -- not build.log -- is the
input: it is structured, versioned and produced even when the run fails.
Two measurements come out of it: dbt_test_results (one row per test) and
dbt_build_runs (one row per run).

QuestDB rather than the duckdb marts because duckdb has no wire protocol
Grafana can reach -- it is an embedded file opened by whoever holds the lock,
so a dashboard cannot read it at all while a build is writing it. QuestDB is
already the ingest sink and already speaks HTTP, so Grafana just points at it.

Usage: publish_run_results.py [run_results_path]. The path defaults to
target/run_results.json beside this script. Environment: QUESTDB_HTTP (default
http://localhost:9001). Exit code: always 0.

This module must never fail a build: every error degrades to a warning. It only
reads run_results.json and writes to QuestDB.

<a id="publish_run_results.esc_tag"></a>

### esc\_tag

```python
def esc_tag(val)
```

ILP tag values cannot carry spaces, commas or equals signs unescaped.

**Arguments**:

- `val` - The tag value (a str).
  

**Returns**:

  The value with spaces, commas and equals signs replaced by underscores.

<a id="publish_run_results.epoch_ns"></a>

### epoch\_ns

```python
def epoch_ns(stamp)
```

Convert a dbt ISO8601 stamp to epoch nanoseconds.

dbt stamps are ISO8601 with a trailing Z, which fromisoformat wants as
an explicit offset before Python 3.11.

**Arguments**:

- `stamp` - An ISO8601 timestamp such as "2026-01-01T00:00:00Z".
  

**Returns**:

  The time as integer nanoseconds since the Unix epoch.
  

**Raises**:

- `ValueError` - If stamp is not a valid ISO8601 timestamp.

<a id="publish_run_results.build_lines"></a>

### build\_lines

```python
def build_lines(run_results)
```

Turn a parsed run_results.json into ILP lines.

Kept separate from the
POST so it can be exercised without a live QuestDB.

**Arguments**:

- `run_results` - The parsed run_results.json; metadata.generated_at is required.
  

**Returns**:

  One dbt_test_results ILP line per test, then one dbt_build_runs line.
  

**Raises**:

- `KeyError` - If metadata.generated_at is missing.

<a id="publish_run_results.publish"></a>

### publish

```python
def publish(path)
```

Read run_results.json and POST its ILP lines to QuestDB.

**Arguments**:

- `path` - Path to dbt's run_results.json.
  

**Returns**:

  A (line_count, http_status) tuple.
  

**Raises**:

- `OSError` - If the file cannot be read.
- `urllib.error.URLError` - If QuestDB is unreachable or rejects the write.

<a id="publish_run_results.main"></a>

### main

```python
def main()
```

Publish the results for argv[1], or the default path.

Always exits 0; a failure is reported as a warning on stderr.

