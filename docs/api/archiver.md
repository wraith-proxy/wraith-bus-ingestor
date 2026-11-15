# Archiver

<a id="archive"></a>

## archive

Moves bus_positions days older than RETENTION_DAYS off the hot QuestDB disk to Parquet.

The days go off the hot QuestDB
disk and onto cold storage as Parquet, instead of being deleted.

Why Parquet rather than QuestDB's own partition files (docs/adr/0007): a
DETACHed QuestDB partition is not self-contained. Its SYMBOL columns are
integer keys into the source table's symbol maps, which are not archived with
it, and QuestDB re-attaches a partition only into the table it came from --
so dropping or recreating that table strands the whole archive (63 days were
lost that way, docs/adr/0006). A Parquet file carries real strings and its own
schema: it can be read by DuckDB straight off the archive drive, or loaded
back into any table.

Why not QuestDB's native `SET TTL`: TTL drops partitions outright. This
exports the day first, proves the file, and only then drops the live copy.
The order matters -- a day is never dropped until its Parquet file has been
read back and matches QuestDB row for row.

Each run also records itself in QuestDB (`archiver_runs`), which is what the
Grafana alert rules read: Grafana can see QuestDB, not this drive.

This module must never drop a partition before its Parquet file has been read
back and verified, and must never write unless the archive sentinel file is
present.

Environment:
    QUESTDB_URL, RETENTION_DAYS, ARCHIVE_OWNER (uid:gid for written files).

<a id="archive.log"></a>

### log

```python
def log(msg)
```

Log a line to stdout and the archive log file.

**Arguments**:

- `msg` - Message to log.

<a id="archive.qdb_exec"></a>

### qdb\_exec

```python
def qdb_exec(query, timeout=30)
```

Run a SQL statement against QuestDB's /exec endpoint.

**Arguments**:

- `query` - SQL text.
- `timeout` - Request timeout in seconds.
  

**Returns**:

  The decoded JSON response.
  

**Raises**:

- `urllib.error.URLError` - QuestDB is unreachable or returns an HTTP error.

<a id="archive.attached_partitions"></a>

### attached\_partitions

```python
def attached_partitions()
```

List [(name, min_timestamp), ...] for the live table's partitions, oldest first.

These are the partitions currently in the live
table, oldest first. A name that doesn't parse as a date is not ours to
touch -- this table is PARTITION BY DAY.

**Returns**:

  A list of (name, min_timestamp) tuples.

<a id="archive.live_rows"></a>

### live\_rows

```python
def live_rows(name)
```

Count the rows QuestDB holds for one day.

**Arguments**:

- `name` - Partition name, a YYYY-MM-DD date.
  

**Returns**:

  The live row count.

<a id="archive.export_day"></a>

### export\_day

```python
def export_day(name, dest)
```

Stream QuestDB's own Parquet export of one day into `dest`.

Streaming:
the archiver's memory does not grow with the day (curl peaked at 11MB on a
34M-row day), and QuestDB peaked at ~4GB of its 8GB doing the same.

**Arguments**:

- `name` - Partition name, a YYYY-MM-DD date.
- `dest` - Local file path to write the Parquet file to.
  

**Raises**:

- `urllib.error.URLError` - The export request failed or timed out.

<a id="archive.parquet_stats"></a>

### parquet\_stats

```python
def parquet_stats(path)
```

Return (rows, min_timestamp, max_timestamp) read back out of the file itself.

This is the proof, as opposed to trusting that the export worked. Timestamps come
back as text: QuestDB writes TIMESTAMPTZ, and fetching one as a Python
datetime needs pytz, which this slim image does not carry (found the hard
way on the first real run, 2026-09-19).

**Arguments**:

- `path` - Path to the Parquet file.
  

**Returns**:

  A tuple of (row count, min timestamp text, max timestamp text).

<a id="archive.drop_partition"></a>

### drop\_partition

```python
def drop_partition(name)
```

Drop one live partition from QuestDB.

**Arguments**:

- `name` - Partition name, a YYYY-MM-DD date.
  

**Returns**:

  True when QuestDB accepted the DDL, False (and an error logged) when not.

<a id="archive.archive_one"></a>

### archive\_one

```python
def archive_one(name)
```

Return 'archived', 'skipped' or 'failed' for one day.

'skipped' means a day left in place
that a human should look at, without blocking the days behind it; 'failed'
stops the run.

**Arguments**:

- `name` - Partition name, a YYYY-MM-DD date.
  

**Returns**:

  One of 'archived', 'skipped' or 'failed'.

<a id="archive.run_once"></a>

### run\_once

```python
def run_once()
```

(status, archived, skipped, detail). status: 'ok' | 'attention' | 'error'.

Archives every attached partition older than RETENTION_DAYS, oldest first.

**Returns**:

  A tuple of (status, archived count, skipped count, detail text).

<a id="archive.record_run"></a>

### record\_run

```python
def record_run(status, archived, skipped, detail)
```

Record one row per run, which is what the Grafana rules read.

A failure to record must
never take the archiver down -- and if QuestDB is unreachable the missing
row is itself the signal (the 'no clean run' rule fires).

**Arguments**:

- `status` - 'ok', 'attention' or 'error'.
- `archived` - Number of days archived this run.
- `skipped` - Number of days skipped this run.
- `detail` - Free text; truncated to 400 characters.

<a id="archive.main"></a>

### main

```python
def main()
```

Run the archive once a day until SIGTERM or SIGINT.

