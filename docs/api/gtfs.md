# GTFS

<a id="build_alias"></a>

## build\_alias

Learn live-label -> GTFS-label aliases from the live feed's own origin/dest refs.

A live (operator, line) that already exists in gtfs_route_patterns needs no
alias. For the rest, a bus's origin_ref/dest_ref pin it to a GTFS pattern
(first_atco/last_atco), which names the GTFS operator+line it really runs.
Never raises on a QuestDB failure: degrades to the previous served table.

This module only reads QuestDB (a bounded 24h query) and writes into the
DuckDB connection it is given; it must never write to QuestDB or to the
previously served database, which it only attaches read-only.

<a id="build_alias.build_alias"></a>

### build\_alias

```python
def build_alias(con, questdb_http, previous_db_path)
```

Build the gtfs_line_alias table on a connection.

Learns aliases from live data, filters them through the review allowlist,
carries over still-valid previous aliases and adds the prefix rules.

**Arguments**:

- `con` - DuckDB connection that already holds gtfs_route_patterns and
  gtfs_agency.
- `questdb_http` - QuestDB HTTP base URL.
- `previous_db_path` - Path to the previously served database, or a falsy
  value when there is none.
  

**Returns**:

  The number of rows in gtfs_line_alias.

<a id="fetch"></a>

## fetch

Weekly refresh of the GB-wide BODS GTFS timetable into a DuckDB file.

Downloads the public feed, loads it into a STAGING DuckDB, runs gtfs/patterns.sql
to derive the serving tables (gtfs_route_patterns, gtfs_route_stops,
gtfs_line_localities), learns gtfs_line_alias from the live feed (build_alias.py,
never fatal), checks the result is sane, and only then atomically renames the staging file over the
live one. A bad or half-vanished feed therefore never replaces a good one.

shapes.txt (2.57GB) is never extracted: OSRM already does geometry.

  fetch.py [--no-record]     --no-record skips the QuestDB run row (test runs)

<a id="fetch.MIN_FREE_BYTES"></a>

#### MIN\_FREE\_BYTES

5.3GB of CSVs + the DB + headroom

<a id="fetch.MIN_ZIP_BYTES"></a>

#### MIN\_ZIP\_BYTES

the real feed is ~1.34GB; an HTML error page is KBs

<a id="fetch.DOWNLOAD_DEADLINE_S"></a>

#### DOWNLOAD\_DEADLINE\_S

a trickle never trips the per-read timeout; this does

<a id="fetch.log"></a>

### log

```python
def log(msg)
```

Log a message to the gtfs logger.

**Arguments**:

- `msg` - Message to log.

<a id="fetch.stage"></a>

### stage

```python
def stage(msg)
```

Log a stage banner.

**Arguments**:

- `msg` - Stage description.

<a id="fetch.download"></a>

### download

```python
def download(dest)
```

Download the GTFS zip and sanity-check it.

**Arguments**:

- `dest` - Path to write the zip to.
  

**Raises**:

- `RuntimeError` - The download exceeded its deadline, was truncated, or is
  not a valid feed zip.
- `urllib.error.URLError` - The request failed.

<a id="fetch.extract"></a>

### extract

```python
def extract(zip_path)
```

Extract the wanted members of the feed zip into the work directory.

**Arguments**:

- `zip_path` - Path to the downloaded zip.
  

**Raises**:

- `RuntimeError` - A required member is missing from the feed.
- `zipfile.BadZipFile` - A member fails its CRC check.

<a id="fetch.read_csv_sql"></a>

### read\_csv\_sql

```python
def read_csv_sql(name, extra_types=None, sample_all=True)
```

read_csv over one member with every *_id column forced to VARCHAR.

**Arguments**:

- `name` - Feed member name without the .txt suffix.
- `extra_types` - Extra column-type overrides, merged over the VARCHAR pins.
- `sample_all` - When true, sample every row for type detection.
  

**Returns**:

  A ``read_csv(...)`` SQL fragment.

<a id="fetch.build_staging"></a>

### build\_staging

```python
def build_staging(con)
```

Load the feed tables and the NaPTAN stops into the staging database.

**Arguments**:

- `con` - DuckDB connection to the staging database.

<a id="fetch.count"></a>

### count

```python
def count(con, table)
```

Count the rows in a table.

**Arguments**:

- `con` - DuckDB connection.
- `table` - Table name.
  

**Returns**:

  The row count.

<a id="fetch.previous_counts"></a>

### previous\_counts

```python
def previous_counts()
```

Row counts of the serving tables in the currently served database.

**Returns**:

  A dict of table name to row count. Empty when there is no previous
  database or it is unreadable; a table the previous database lacks is
  left out.

<a id="fetch.review_accepts"></a>

### review\_accepts

```python
def review_accepts()
```

Keys accepted in alias_review.csv with no reject row (reject wins); same rule as
build_alias._load_review. Unreadable csv gives an empty set: fail closed.

**Returns**:

  A set of (live_operator, live_line, gtfs_operator, gtfs_line) tuples.

<a id="fetch.verify"></a>

### verify

```python
def verify(con)
```

Check the staging database is sane before it may replace the live one.

**Arguments**:

- `con` - DuckDB connection to the staging database.
  

**Returns**:

  A dict of table name to row count.
  

**Raises**:

- `RuntimeError` - Any check failed; the message lists every failure.

<a id="fetch.alias_fallback"></a>

### alias\_fallback

```python
def alias_fallback(con, previous_db)
```

Degrade path: carry the served DB's alias table over, else an empty one. This
stage must never fail a refresh, and an old alias beats none while QuestDB is down.

**Arguments**:

- `con` - DuckDB connection to the staging database.
- `previous_db` - Path to the served database, or None.

<a id="fetch.alias_stage"></a>

### alias\_stage

```python
def alias_stage(con)
```

Learn gtfs_line_alias from the live feed, falling back on any failure.

Never raises: a failure is logged and the previous alias table carried over.

**Arguments**:

- `con` - DuckDB connection to the staging database.

<a id="fetch.write_meta"></a>

### write\_meta

```python
def write_meta(con, n)
```

Write the single-row gtfs_meta table.

**Arguments**:

- `con` - DuckDB connection to the staging database.
- `n` - Table row counts from verify().

<a id="fetch.check_paths"></a>

### check\_paths

```python
def check_paths()
```

Refuse layouts where cleaning WORK could delete the DB (or the home dir).

**Raises**:

- `SystemExit` - The work directory is too broad, or contains the database.

<a id="fetch.clean_work"></a>

### clean\_work

```python
def clean_work()
```

Delete only what this script creates, never the directory itself.

<a id="fetch.refresh"></a>

### refresh

```python
def refresh(staging)
```

Download, build, verify and publish a fresh database.

**Arguments**:

- `staging` - Path of the staging database file.
  

**Returns**:

  A dict of table name to row count from verify().
  

**Raises**:

- `RuntimeError` - Not enough free space, a bad download, or failed
  verification. The served database is left untouched.

<a id="fetch.record_run"></a>

### record\_run

```python
def record_run(status, n, started, message)
```

One row per run for Grafana. Mirrors archiver.record_run: a failure to
record is logged and swallowed, never allowed to mask the real outcome.

**Arguments**:

- `status` - 'ok' or 'error'.
- `n` - Row counts from verify(), or None.
- `started` - Run start time, from time.time().
- `message` - Free text; truncated to 400 characters.

<a id="fetch.main"></a>

### main

```python
def main()
```

Run one refresh.

Takes a lock so only one refresh runs, cleans staging on any failure and
records the outcome in QuestDB unless ``--no-record`` was given.

**Raises**:

- `SystemExit` - Bad arguments, an unsafe path layout, or another refresh
  holds the lock.

