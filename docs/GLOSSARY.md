# Bus Platform

Ingests live bus positions for Great Britain, joins them to published timetables, and serves lateness and arrival predictions. This glossary fixes the words the contracts, ADRs and semantic layer use.

## Language

### Positions and vehicles

**Position report**:
One raw row from the live feed: where a vehicle says it is, polled about every 60 seconds.
_Avoid_: ping (for the raw row), reading, poll

**Ping**:
One vehicle's usable position in a 3-minute bucket; the unit segmentation works on.
_Avoid_: position report (for the bucketed row)

**Vehicle**:
A physical bus, identified by operator plus bus id.
_Avoid_: bus (in model and metric names)

**Operator**:
The company running a vehicle, identified by its National Operator Code. Pooled estimates use `*` for "all operators"; rows from before the operator field existed form their own unnamed stream.
_Avoid_: company, NOC (as a synonym)

### Services and timetables

**Line**:
The service identity painted on the bus, such as "42".
_Avoid_: route, service number

**Pattern**:
An ordered sequence of stops that a line follows in the published timetable. Pattern ids are not stable across timetable refreshes.
_Avoid_: route, route shape

**Trip**:
One scheduled run of a pattern in the published timetable.
_Avoid_: journey (for scheduled runs)

**Journey**:
An operator's working of a line on a given day, identified by the operator's journey reference plus origin and aimed departure.
_Avoid_: working, run (for the operator's concept)

**Run**:
An observed stretch of one vehicle's pings, cut out by segmentation; attributed to a journey when the feed supplies a journey reference.
_Avoid_: journey (for the observed stretch), segment

**Published timetable**:
The operators' timetables as published to the national open data service.
_Avoid_: schedule, GTFS (as a synonym)

**Inferred route**:
The legacy approach of deriving stop sequences from observed pings. Retired in favour of published timetables.

### Lateness and prediction

**Delay**:
The signed live measurement of how late (positive) or early (negative) a vehicle is. Null where progress cannot be computed.
_Avoid_: lateness (for the live value)

**Journey-end lateness**:
How late a journey was when it finished; the measurement behind the punctuality mart.
_Avoid_: delay

**ETA residual**:
The error of the arrival-time regression against what was observed. It never colours the map.
_Avoid_: delay

### Time and storage

**Hot window**:
The days of raw positions kept on the fast disk in the live database (3 days).
_Avoid_: retention (as a noun for the window)

**Extract window**:
The days of raw positions exported nightly to Parquet for the build (4 days).

**History window**:
The days of per-day summaries kept after raw positions age out (8 days).

**Archive**:
Cold Parquet copies of old days on the bulk disk. "Glacier" is informal slang for the same cold storage.
_Avoid_: backup

### Data layers

**Data product**:
A governed dataset with a contract. Four kinds, in dependency order: raw (RDP), origin (ODP), consumption (CDP), functional (FDP). Not every flow uses all four.
_Avoid_: layer, tier

**Raw data product (RDP)**:
Data exactly as received from the source.

**Origin data product (ODP)**:
Data cleaned and typed from the raw source, still shaped like the source.

**Consumption data product (CDP)**:
The staging layer: typed, filtered, per-row-derived tables built from the raw extract, which everything downstream reads.
_Avoid_: staging table

**Functional data product (FDP)**:
The presentation layers above staging: the intermediate models and the marts the API reads. "Mart" is accepted shorthand for the final, API-facing FDP tables.
_Avoid_: PRES (as a layer name), presentation layer
