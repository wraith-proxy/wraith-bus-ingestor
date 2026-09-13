# Contracts

`contracts/` documents the two boundaries in the bus platform as
[Open Data Contract Standard v3.2.0](https://bitol-io.github.io/open-data-contract-standard/v3.2.0/)
specs, plus a Foundry-style ontology layered over both. Validate all three
against live data with:

```bash
python contracts/validate.py
```

Exit code 0 means every schema, key, and quality rule holds; 1 gates a
build or a PR. `contracts/validate.py --marts /path/to/candidate.duckdb`
checks a not-yet-published marts file, which is how a rebuild could be
gated before `dbt/build.sh` moves it into place.

## `bus-positions.odcs.yaml` — the ingest boundary

**Producer:** `ingestor/ingest.py`. **Consumers:** `dbt/extract.py` and the
Flask API, which both read `bus_positions` directly.

This contract is as much about what the feed does **not** guarantee as
what it does:

- **The feed republishes dead records.** A vehicle that finished for the
  day keeps appearing with its last known position and original
  `RecordedAtTime`. Measured live: 32% of vehicles carry a position over 5
  minutes stale, 29% over 30 minutes, out to a full 24 hours. Consumers
  must judge freshness on `recorded_at`, never on `timestamp` (which is
  only when *this row* was written).
- **Vehicles stay logged onto a finished journey** while starting the
  next, so the aimed times on a row can belong to a working the bus
  completed an hour ago.
- **`bus_id` is not globally unique** — short numeric references like
  `3104` are reused by unrelated operators in different towns.
- **Schedule columns are recent.** `origin_ref`, `dest_aimed_arr`,
  `journey_ref` and others were added on 2026-09-12; every row before that
  has them permanently `NULL`, not merely "sometimes missing".

The contract's one hard quality gate: fewer than 1,000 rows in a poll means
the ingestor has died or the BODS API key has lapsed, not that the country
ran out of buses.

## `bus-marts.odcs.yaml` — the analytics boundary

**Producer:** the `ghost_bus` dbt project. **Consumer:** `backend/app.py`,
which opens the file read-only. The API is not allowed to read anything in
`ghost_bus.duckdb` that isn't described here — staging and intermediate
models are the producer's private business and may change without notice;
anything in this contract may only change via a version bump.

Five tables:

| Table | What it is |
|---|---|
| `eta_models` | Fitted OLS coefficients, one row per grain plus fallback rows — see [Bus Platform](bus-platform.md#the-eta-model) |
| `line_stops` | Stop sequence per `(operator, line, direction)` |
| `line_localities` | Which localities each line passes through |
| `line_punctuality` | Observed delay distribution per line/grain |
| `localities` | Locality gazetteer powering `/search` |

Key quality rules: `eta_models` must have >500 rows and no duplicate
`(operator, line, direction, day_type, hour_band)` keys (the API takes the
first hit on a lookup, so a duplicate makes the answer arbitrary); the
global fallback row (`operator='*', line='*', direction='*', day_type='*',
hour_band='*'`) must exist exactly once, since it's the last rung of the
API's fallback chain; and every fitted grain's implied speed must fall in a
plausible range, so a regression bug can't silently publish a
mathematically-valid but physically-absurd prediction.

The contract's `limitations` section calls out what's structural, not
fixable in this layer: `line` is a published number, not a unique route
(the same "88" exists in unrelated towns); day classification has no
concept of bank holidays; the fitted window has a real gap (2026-08-28 to
08-30, a feed outage).

## `ghost-bus.ontology.yaml` — the semantic layer

Modelled the way Palantir Foundry structures an ontology — shared value
types, object types with a primary key and title property, typed link
types with explicit cardinality — because there's no open standard for
this the way ODCS covers the contract layer itself. Every ontology property
binds to a contract + schema object + property, so `validate.py` can prove
the ontology hasn't drifted from the data it describes.

**Value types** — semantic primitives declared once so a concept can't be
typed two different ways in two different places: `AtcoCode` (NaPTAN stop
ID, pattern-constrained), `ServiceNumber` (deliberately *not* a route
identifier — the same number is reused across towns), `LocalityNorm`,
`Direction`, `DayType`, `HourBand`, `RagStatus`, `DelaySeconds`,
`EpochMillis`.

**Object types** — the things in this domain: `Vehicle`, `Journey`,
`Line`, `Operator`, `Stop`, `LineStop`, `Locality`, `TravelTimeModel`,
`PunctualityProfile`. Each carries a primary key and binds every property
back to a contract column.

**Link types** — typed relationships with real cardinality, e.g.
`vehicleOperatedBy` (many vehicles to one operator), `vehicleWorking` (a
vehicle to the journey it's currently running), `journeyOnLine`,
`journeyStartsAt`. This is what turns "a table with a `line` column" into
"a Journey runs on exactly one Line" — a claim `validate.py` can check
against the actual data, not just assert in prose.

The contracts say what the **columns** are; the ontology says what the
**things** are.
