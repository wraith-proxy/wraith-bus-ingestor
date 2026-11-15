# Ops

<a id="compile_metrics"></a>

## compile\_metrics

Compile the Ossie metrics in contracts/metrics.ossie.yaml to MetricFlow yaml.

    ops/compile_metrics.py [--ossie FILE] [--out FILE]

Deterministic: sorted keys, no timestamps, no locale or hash-seed dependence.
Only validly aggregable metrics compile: no percentile or median, and any metric over
the eta_models rollup table must filter `operatorRef != '*'`. Marts have no time column,
so no semantic model carries an agg_time_dimension (ticket `52`). A composite primary
key becomes one concatenated primary entity (the converter rejects composite keys).

This module reads only the Ossie yaml and writes only the generated MetricFlow
file. It must never touch a database or hand-edit the generated output.

<a id="compile_metrics.CompileError"></a>

### CompileError

```python
class CompileError(Exception)
```

Raised when a metric cannot be compiled to a valid MetricFlow metric.

<a id="compile_metrics.compile_model"></a>

### compile\_model

```python
def compile_model(model)
```

Compile an Ossie model to MetricFlow yaml text.

**Arguments**:

- `model` - The parsed Ossie document, with `datasets` and `metrics`.
  

**Returns**:

  The generated yaml text, header included, deterministic for a given model.
  

**Raises**:

- `CompileError` - If a metric is a percentile, has an unsupported expression,
  names an undeclared field, reads a non-mart, or lacks the real-operator
  filter on the eta_models rollup.

<a id="compile_metrics.main"></a>

### main

```python
def main(argv=None)
```

Compile the Ossie metrics file to the MetricFlow yaml file.

**Arguments**:

- `argv` - Command-line arguments; defaults to `sys.argv[1:]`.
  

**Returns**:

  0 on success, 1 on a `CompileError`.

<a id="cutover_compare"></a>

## cutover\_compare

Compare two DuckDB warehouses table by table (ops/cutover.sh stage 4).

Exact: row counts, and every non-DOUBLE column (the keys and everything else;
EXCEPT both ways). Tolerant: DOUBLE columns, because DuckDB's 3-thread sums
are not bit-reproducible (profiles.yml threads: 3). Rows are paired on the
non-DOUBLE columns, so a table whose non-DOUBLE columns repeat is compared
as a multiset of those columns and may need a `keys` override.

    cutover_compare.py BASELINE.duckdb CANDIDATE.duckdb TABLE [TABLE ...]

Exit 0 identical within tolerance, 1 otherwise. Opens both files read-only.

This module must never write to either warehouse.

<a id="cutover_compare.Result"></a>

### Result

```python
@dataclass
class Result()
```

Outcome of a comparison.

**Attributes**:

- `problems` - One message per mismatch found; empty means identical.
- `ok` - True when there are no problems.

<a id="cutover_compare.Result.ok"></a>

#### ok

```python
@property
def ok()
```

Whether the comparison found no problems.

<a id="cutover_compare.compare"></a>

### compare

```python
def compare(baseline, candidate, tables, atol=ATOL, rtol=RTOL, keys=None)
```

Compare two DuckDB warehouses table by table.

**Arguments**:

- `baseline` - Path to the baseline warehouse file.
- `candidate` - Path to the candidate warehouse file.
- `tables` - Table names to compare.
- `atol` - Absolute tolerance for DOUBLE columns.
- `rtol` - Relative tolerance for DOUBLE columns.
- `keys` - Optional mapping of table name to the columns that pair rows,
  overriding the default of every non-DOUBLE column.
  

**Returns**:

  A `Result` listing every mismatch found.

<a id="cutover_compare.main"></a>

### main

```python
def main(argv)
```

Compare the tables named on the command line and print the verdict.

**Arguments**:

- `argv` - BASELINE, CANDIDATE, then one or more table names.
  

**Returns**:

  0 if identical within tolerance, 1 on mismatch, 2 on too few arguments.

<a id="incident"></a>

## incident

Recover bus_positions after an incident, by restore or replay.

Restore an archived day back into live QuestDB, or replay a whole day range
through extract+dbt.

Runs on the host (not in a container) -- it needs both `docker` (to reach
into questdb's uid-10001-owned partition dir, same workaround used for
server.conf edits) and this repo's own dbt/.venv (to reuse extract.py's
already-tested chunked-fetch logic rather than reimplementing it).

    ops/incident.py restore 2026-08-02
    ops/incident.py replay 2026-08-01 2026-08-03

restore is idempotent: a day already attached is a no-op, not an error.
replay is built on restore, not a copy of it -- each day in range gets
restore()'d if it's outside the hot RETENTION_DAYS window, then extracted
via the same fetch() extract.py's own nightly run uses, then one
SKIP_EXTRACT=1 FULL_REFRESH=1 build.sh call covers the whole range.

replay only helps for days that still exist in cold storage. A day lost
INSIDE the hot RETENTION_DAYS window cannot be recovered by anything here:
extract_day() re-reads that day from QuestDB, which is exactly where the
rows are already missing, and BODS serves live positions only -- there is
no backfill API to go back to. That case needs the archive, or it is gone.

Depends on bus_positions' DEDUP UPSERT KEYS(bus_id, timestamp) (enabled
2026-09-15, see docs/ROADMAP.md 1.3.0) -- without it, replaying a day that
was only partially lost (not fully missing) double-counts the rows that
were already there.

The archive is Parquet (docs/adr/0007): one self-describing file per day in
`${ARCHIVE_PATH}/bus_positions_parquet/<day>.parquet`, real strings and its
own schema. restore() copies the file into QuestDB's import directory and
INSERTs it back through read_parquet(); the archiver then finds the day already
archived, checks the file against the live rows, and drops the live copy. A
day can also be read in place, no restore needed:

    duckdb -c "SELECT ... FROM read_parquet('<archive>/bus_positions_parquet/*.parquet')"

63 days (2026-07-03 .. 2026-09-06) are numeric-only: their bus_id/line symbol
maps were lost in the 2026-09-18 schema migration (docs/adr/0006). They were
salvaged to Parquet with `timestamp lat lon bearing` and the raw integer keys,
and restore() refuses them -- there is no bus_id to load into bus_positions.

This is a host-side operator tool. It must never be run inside a container,
never recover a day lost inside the hot window (see above), and never restore
a numeric-only archive.

<a id="incident.qdb_exec"></a>

### qdb\_exec

```python
def qdb_exec(query, timeout=30)
```

Run a query against QuestDB's HTTP /exec endpoint.

**Arguments**:

- `query` - SQL text to execute.
- `timeout` - Seconds to wait for the HTTP response.
  

**Returns**:

  The decoded JSON response.

<a id="incident.docker"></a>

### docker

```python
def docker(*args, check=True)
```

Run a docker CLI command and capture its output.

**Arguments**:

- `*args` - Arguments passed to `docker`.
- `check` - Raise on a non-zero exit when True.
  

**Returns**:

  The completed process, with text stdout and stderr captured.
  

**Raises**:

- `subprocess.CalledProcessError` - If `check` is True and docker exits non-zero.

<a id="incident.is_attached"></a>

### is\_attached

```python
def is_attached(day, table=TABLE)
```

Report whether a day's partition is attached in QuestDB.

**Arguments**:

- `day` - Day as YYYY-MM-DD.
- `table` - Table whose partitions are checked.
  

**Returns**:

  True if the partition is attached.

<a id="incident.COLUMNS"></a>

#### COLUMNS

bus_positions' columns in table order (docs/adr/0006)

<a id="incident.parquet_info"></a>

### parquet\_info

```python
def parquet_info(path)
```

(column names, row count) read from the file itself.

**Arguments**:

- `path` - Path to the Parquet file.
  

**Returns**:

  A tuple of the column names and the row count.

<a id="incident.restore"></a>

### restore

```python
def restore(day)
```

Restore a day from the Parquet archive into live bus_positions.

Brings a day back from the Parquet archive into live bus_positions.
No-op if already present. The row count is checked against the file.

**Arguments**:

- `day` - Day as YYYY-MM-DD.
  

**Raises**:

- `RuntimeError` - If there is no Parquet archive for the day, the archive is
  numeric-only (no bus_id), the INSERT fails, or the restored row
  count does not match the file.

<a id="incident.extract_day"></a>

### extract\_day

```python
def extract_day(day_obj)
```

Pull one day of rows out of QuestDB using extract.py.

Uses extract.py's own build_select/fetch -- same
chunking, same column typing, same empty-file handling as the nightly
run. Reimporting rather than reimplementing: this is the one place that
logic should live.

**Arguments**:

- `day_obj` - The day to extract, as a `datetime.date`.
  

**Returns**:

  The total number of rows extracted across the day's chunks.

<a id="incident.daterange"></a>

### daterange

```python
def daterange(start, end)
```

Yield each day from start to end inclusive.

**Arguments**:

- `start` - First day, as a `datetime.date`.
- `end` - Last day, as a `datetime.date`.
  

**Yields**:

  Each `datetime.date` in the range.

<a id="incident.replay"></a>

### replay

```python
def replay(start_str, end_str)
```

Replay a day range through restore (if needed), extract and dbt.

Days older than the hot RETENTION_DAYS window are restored first, then every
day is extracted, then one full-refresh build.sh run covers the range.

**Arguments**:

- `start_str` - First day as YYYY-MM-DD.
- `end_str` - Last day as YYYY-MM-DD.
  

**Raises**:

- `RuntimeError` - If start is after end, or a restore fails.
- `subprocess.CalledProcessError` - If build.sh exits non-zero.

<a id="incident.main"></a>

### main

```python
def main()
```

Parse the command line and run `restore` or `replay`.

<a id="publish_marts_state"></a>

## publish\_marts\_state

Publish marts_state health into QuestDB so Grafana can alert on it.

The marts-state API (slaycoach.quest) reports whether the duckdb marts are
readable ('ok'), locked by a concurrent writer, missing, unreadable, or
lacking a driver. QuestDB has no way to poll that API itself, and duckdb
has no wire protocol Grafana can reach directly, so this script is the
bridge: fetch the API's verdict, write one row to QuestDB, and let systemd's
exit-code tracking surface anything other than 'ok' in the logs.

Unlike dbt/publish_run_results.py, which republishes a timestamp already
recorded in run_results.json, the marts-state API carries no timestamp of
its own -- so this script stamps the row with wall-clock now at publish time.
An unreachable API is itself the signal this exists to catch, so a failed
GET still publishes a row (state="unreachable", n_models=0) rather than
skipping the publish.

This module must never read the duckdb marts itself; it only talks to the
marts-state API and QuestDB's HTTP endpoint.

<a id="publish_marts_state.esc_tag"></a>

### esc\_tag

```python
def esc_tag(val)
```

ILP tag values cannot carry spaces, commas or equals signs unescaped.

**Arguments**:

- `val` - Raw tag value.
  

**Returns**:

  The value with spaces, commas and equals signs replaced by underscores.

<a id="publish_marts_state.fetch_state"></a>

### fetch\_state

```python
def fetch_state()
```

Return the marts state and model count, degrading to unreachable on failure.

Returns (state, n_models). Any failure degrades to ("unreachable", 0)
rather than raising, since the API being down is itself worth a row --
but the real exception is printed first (see main()'s caller), because
silently collapsing every failure into "unreachable" hid a real bug: no
User-Agent header meant a bare `python3 ops/publish_marts_state.py` got
a 403 from whatever's in front of the public URL (Cloudflare or nginx
treating urllib's default `Python-urllib/x.y` UA as a bot), while a
plain `curl` to the exact same URL passed -- caught 2026-09-24, after
the timer had been "working" (publishing a row every 5 minutes, just
always the wrong one) since it was first enabled.

**Returns**:

  A tuple of the state string and the model count.

<a id="publish_marts_state.build_line"></a>

### build\_line

```python
def build_line(state, n_models, ts_ns)
```

Build a single ILP line from a state reading.

Kept separate from the
POST so it can be exercised without a live QuestDB.

**Arguments**:

- `state` - The marts state reading.
- `n_models` - Number of models reported.
- `ts_ns` - Timestamp in nanoseconds since the epoch.
  

**Returns**:

  The ILP line.

<a id="publish_marts_state.publish"></a>

### publish

```python
def publish(line)
```

POST one ILP line to QuestDB.

**Arguments**:

- `line` - The ILP line to write.
  

**Returns**:

  The HTTP status code.

<a id="publish_marts_state.main"></a>

### main

```python
def main()
```

Fetch the state, publish a row, and exit 0 only when the state is 'ok'.

<a id="ready_gate"></a>

## ready\_gate

Validate the extract window against its contract and write the readiness marker.

Ready gate: validate the whole extract window against its contract, then write
the readiness marker only when every error-severity check passes. Warning-severity
rules never block. A failed validation never writes the marker.

This module reads Parquet extracts with an in-memory DuckDB and writes only the
marker file. It must never touch a live QuestDB or the warehouse, and it must
never write the marker after a failed check.

<a id="ready_gate.validate"></a>

### validate

```python
def validate(contract: Path, data_dir: Path, marker: Path) -> bool
```

True and marker written when the extract meets the contract; else False, no marker.

**Arguments**:

- `contract` - Path to the ODCS contract YAML.
- `data_dir` - Directory holding the extract Parquet files.
- `marker` - Path of the readiness marker to write on success.
  

**Returns**:

  True if every error-severity check passed and the marker was written,
  False otherwise (nothing written).

<a id="salvage_legacy_archive"></a>

## salvage\_legacy\_archive

One-off: salvage the 63 archived days whose symbol maps were lost into Parquet.

Kept for provenance, not routine use (docs/adr/0006, docs/adr/0007). Days
archived before the 2026-09-18 schema migration hold `bus_id` and `line` as
integer keys into the old table's symbol maps, which were dropped with it, so
they can never be decoded again. What is still real is the numeric columns.
This reads those raw QuestDB column files directly and writes one Parquet file
per day: `timestamp`, `lat`, `lon`, `bearing`, plus the raw `bus_id_key` /
`line_key` integers (meaningless without the lost maps, kept so nothing that
survived is thrown away). The file's key/value metadata says so.

    uv run --no-project --with numpy --with pyarrow --with duckdb \
        python ops/salvage_legacy_archive.py            # every old-layout day
        python ops/salvage_legacy_archive.py 2026-08-31 # one day

Run once, on 2026-09-19. The raw archive it read (`bus_positions/`) was deleted
afterwards, once every output had been independently recounted, so this can no
longer be re-run; it is kept to show how the 63 files were made. It never
modified the raw archive. A day is skipped if its Parquet file already exists.

QuestDB pre-sizes column files, so each holds a short run of zero padding after
the real rows. It is trimmed, and every assumption is asserted rather than
trusted: the zeros are contiguous at the tail, the real rows are monotonic and
inside the day, and the real row count appears in the partition's own `_txn`.

This module must never modify the raw archive and never overwrite an existing
Parquet file.

<a id="salvage_legacy_archive.column"></a>

### column

```python
def column(day_dir, name, dtype)
```

Read one raw QuestDB column file from a partition directory.

**Arguments**:

- `day_dir` - Partition directory for the day.
- `name` - Column name.
- `dtype` - NumPy dtype string of the stored values.
  

**Returns**:

  The column's values as a NumPy array, padding included.

<a id="salvage_legacy_archive.is_legacy"></a>

### is\_legacy

```python
def is_legacy(day_dir)
```

Report whether a partition uses the old layout (has `vehicle_id` files).

**Arguments**:

- `day_dir` - Partition directory for the day.
  

**Returns**:

  True for an old-layout day.

<a id="salvage_legacy_archive.salvage_day"></a>

### salvage\_day

```python
def salvage_day(day_dir, dest)
```

Write `dest` from the raw partition at `day_dir`; return the row count.

**Arguments**:

- `day_dir` - Raw partition directory, named for the day.
- `dest` - Parquet file to write, via a `.part` file and an atomic rename.
  

**Returns**:

  The number of real rows written.
  

**Raises**:

- `AssertionError` - If any layout assumption fails (no rows, padding not at
  the tail, rows out of order or outside the day, row count absent
  from `_txn`).

<a id="salvage_legacy_archive.main"></a>

### main

```python
def main(days)
```

Salvage the given days, or every old-layout day when none are given.

**Arguments**:

- `days` - Day names (YYYY-MM-DD) to salvage; empty or None means all.

<a id="sla_check"></a>

## sla\_check

SLA checks for a contract's slaProperties, plus the data-as-of stamp.

This module reads extract Parquet and writes only the staged warehouse file it is
given and the served path. It must never touch a live QuestDB, and an SLA breach
must never fail a completed publish.

<a id="sla_check.Breach"></a>

### Breach

```python
@dataclass(frozen=True)
class Breach()
```

One SLA breach.

**Attributes**:

- `property` - The slaProperties name that was breached.
- `message` - Human-readable description of the breach.

<a id="sla_check.check_slas"></a>

### check\_slas

```python
def check_slas(contract: Path, now: datetime, *,
               as_of: datetime) -> list[Breach]
```

Breaches of the contract's `latency` SLA: data older than the allowed age.

**Arguments**:

- `contract` - Path to the ODCS contract YAML.
- `now` - The current time.
- `as_of` - When the data is as of.
  

**Returns**:

  A list of breaches, empty if the SLA holds.

<a id="sla_check.write_meta"></a>

### write\_meta

```python
def write_meta(db: Path, as_of: datetime, version: str) -> None
```

Stamp the warehouse with when its data is as of and which contract version built it.

**Arguments**:

- `db` - Path to the DuckDB warehouse file.
- `as_of` - When the data is as of.
- `version` - Contract version string.

<a id="sla_check.data_as_of"></a>

### data\_as\_of

```python
def data_as_of(db: Path) -> datetime
```

The data-as-of stamp stored in the warehouse's `_meta` table.

**Arguments**:

- `db` - Path to the DuckDB warehouse file.
  

**Returns**:

  The stored timestamp.

<a id="sla_check.contract_version"></a>

### contract\_version

```python
def contract_version(db: Path) -> str
```

The contract version stored in the warehouse's `_meta` table.

**Arguments**:

- `db` - Path to the DuckDB warehouse file.
  

**Returns**:

  The stored contract version.

<a id="sla_check.publish"></a>

### publish

```python
def publish(staged: Path, served: Path, as_of: datetime, version: str) -> None
```

Stamp the staged warehouse, then swap it over the served file atomically.

The stamp goes INTO the file before the rename, so a sidecar file can never
disagree with what is served. If stamping fails, the served file is untouched.

**Arguments**:

- `staged` - Path to the staged warehouse file.
- `served` - Path of the file being served; replaced atomically.
- `as_of` - When the data is as of.
- `version` - Contract version string.

<a id="sla_check.extract_data_as_of"></a>

### extract\_data\_as\_of

```python
def extract_data_as_of(data_dir: Path) -> datetime
```

Newest ping in the extract Parquet: what the served marts are as of.

**Arguments**:

- `data_dir` - Directory holding the extract Parquet files.
  

**Returns**:

  The newest timestamp, timezone-aware (UTC if the data was naive).
  

**Raises**:

- `ValueError` - If the extract holds no pings.

<a id="sla_check.main"></a>

### main

```python
def main(argv=None) -> int
```

`publish STAGED SERVED --data-dir DIR --contract FILE`: stamp, swap, report breaches.

Called by dbt/build.sh in place of a bare `mv`. A failure to stamp leaves the served file
untouched (the build stops, the previous marts keep serving). An SLA breach is reported
but does not fail the publish: it is a ticket for the owner, not a reason to go dark.

**Arguments**:

- `argv` - Command-line arguments; defaults to `sys.argv[1:]`.
  

**Returns**:

  0 always once the publish has completed.

<a id="sql_ratchet"></a>

## sql\_ratchet

SQL ratchet: fail CI when hand-written dbt SQL drifts from the baseline.

Counts dbt/models, dbt/macros and dbt/tests (generated/ subtrees excluded, the
drift test covers those). Gate: non-blank code lines (SQL block comments, Jinja
{# #} comments and -- line comments stripped). Growth fails; a measured count
below the baseline also fails until the baseline is lowered, so shrunk SQL cannot
quietly regrow. Lower it with: python3 ops/sql_ratchet.py --update
Raw line count is recorded in the baseline for context but does not gate.

This module reads .sql files and the baseline file. It must never run SQL or
touch a database, and it only writes the baseline under --update.

<a id="sql_ratchet.strip_comments"></a>

### strip\_comments

```python
def strip_comments(text)
```

Remove comments but keep line breaks, so line counting stays aligned.

**Arguments**:

- `text` - SQL source text.
  

**Returns**:

  The text with block, Jinja and line comments removed.

<a id="sql_ratchet.code_lines"></a>

### code\_lines

```python
def code_lines(text)
```

Count non-blank lines of code once comments are stripped.

**Arguments**:

- `text` - SQL source text.
  

**Returns**:

  The number of non-blank code lines.

<a id="sql_ratchet.raw_lines"></a>

### raw\_lines

```python
def raw_lines(text)
```

Count all lines of text.

**Arguments**:

- `text` - SQL source text.
  

**Returns**:

  The raw line count.

<a id="sql_ratchet.sql_files"></a>

### sql\_files

```python
def sql_files(root)
```

List the hand-written .sql files under a root.

**Arguments**:

- `root` - Directory to search; any `generated` subtree is excluded.
  

**Returns**:

  A sorted list of paths.

<a id="sql_ratchet.measure"></a>

### measure

```python
def measure(*roots)
```

Return (code_lines, raw_lines) summed over every hand-written .sql under roots.

**Arguments**:

- `*roots` - Directories to measure.
  

**Returns**:

  A tuple of code lines and raw lines.

<a id="sql_ratchet.read_baseline"></a>

### read\_baseline

```python
def read_baseline(path)
```

Return (code, raw) from a file of key=value lines; '#' starts a comment.

**Arguments**:

- `path` - Baseline file path.
  

**Returns**:

  A tuple of the code and raw counts.

<a id="sql_ratchet.check"></a>

### check

```python
def check(roots, baseline)
```

Return (ok, message). ok is False when code lines differ from the baseline.

**Arguments**:

- `roots` - A directory or an iterable of directories to measure.
- `baseline` - Baseline file path.
  

**Returns**:

  A tuple of the pass flag and a message.

<a id="sql_ratchet.write_baseline"></a>

### write\_baseline

```python
def write_baseline(path, code, raw)
```

Write the baseline file, keeping any existing '#' header lines.

**Arguments**:

- `path` - Baseline file path.
- `code` - Code line count.
- `raw` - Raw line count.

<a id="sql_ratchet.main"></a>

### main

```python
def main(argv=None)
```

Check the SQL line counts against the baseline, or rewrite it with --update.

**Arguments**:

- `argv` - Command-line arguments; defaults to `sys.argv[1:]`.
  

**Returns**:

  0 on success, 1 when the counts differ from the baseline.

