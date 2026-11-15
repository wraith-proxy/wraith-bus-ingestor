# Ingestor

<a id="ingest"></a>

## ingest

Poll the BODS SIRI-VM feed and write vehicle positions to QuestDB over ILP/HTTP.

This is the long-running ghost-ingestor process. It only appends rows to
bus_positions; it must never read back from, alter or delete anything in
QuestDB, and it must never log the BODS API key.

Environment:
    BODS_API_KEY: BODS API key (required).
    QUESTDB_URL: QuestDB HTTP base URL (required).

<a id="ingest.clean"></a>

### clean

```python
def clean(sym)
```

Make a value safe as an ILP symbol, or return the placeholder.

**Arguments**:

- `sym` - Raw symbol, possibly None or empty.
  

**Returns**:

  The symbol with spaces and commas replaced by underscores, or "UKN"
  when it is empty.

<a id="ingest.esc_tag"></a>

### esc\_tag

```python
def esc_tag(val)
```

ILP tag values cannot carry spaces, commas or equals signs unescaped.

**Arguments**:

- `val` - Tag value to escape.
  

**Returns**:

  The value with those characters replaced by underscores.

<a id="ingest.esc_str"></a>

### esc\_str

```python
def esc_str(val)
```

ILP string field values are double-quoted; escape quotes and backslashes.

**Arguments**:

- `val` - String value to escape.
  

**Returns**:

  The escaped value.

<a id="ingest.text"></a>

### text

```python
def text(el, path)
```

Return the stripped text of a SIRI child element.

**Arguments**:

- `el` - Parent XML element.
- `path` - ElementTree path relative to ``el``, using the siri namespace.
  

**Returns**:

  The stripped text, or None when the element or its text is missing.

<a id="ingest.epoch_ms"></a>

### epoch\_ms

```python
def epoch_ms(stamp)
```

SIRI timestamps are ISO8601, usually with a +00:00 offset.

**Arguments**:

- `stamp` - ISO8601 timestamp string, or None.
  

**Returns**:

  Milliseconds since the epoch, or None when empty or unparseable.

<a id="ingest.build_line"></a>

### build\_line

```python
def build_line(v)
```

One ILP line per VehicleActivity, or None if it lacks a usable position.

**Arguments**:

- `v` - A siri VehicleActivity XML element.
  

**Returns**:

  The ILP line for the bus_positions measurement.
  

**Raises**:

- `AttributeError` - A required element (VehicleRef, Latitude, Longitude,
  LineRef) is missing.
- `TypeError` - A required element has no text.
- `ValueError` - Latitude or longitude is not a number.

<a id="ingest.ingest"></a>

### ingest

```python
def ingest()
```

Returns True on success (an empty feed still counts), False on a
failure that should count toward the backoff in main().

**Returns**:

  True when the poll succeeded, False when it failed.

<a id="ingest.main"></a>

### main

```python
def main()
```

Run the poll loop until SIGTERM or SIGINT, backing off on failures.

