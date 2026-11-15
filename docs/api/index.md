# Python API

The reference for every public Python module in the repo, one page per area. It is
generated from the modules' own Google-style docstrings each time the docs are built,
so it describes the code as it stands. Private helpers (leading underscore) are left
out.

| Area | What is in it |
| --- | --- |
| [Backend](backend.md) | The live bus API (`app`), the GTFS timetable reader, response schemas and the tiebreak rule. |
| [Ops](ops.md) | Operator scripts: SLA and ready gates, cutover comparison, incident handling, the SQL ratchet, marts publishing. |
| [Contracts](contracts.md) | The contract validator and the policy gate for contract changes. |
| [GTFS](gtfs.md) | Fetching GTFS feeds and building the alias table. |
| [Archiver](archiver.md) | The Parquet archive writer. |
| [Ingestor](ingestor.md) | The BODS feed ingestor. |
| [dbt scripts](dbt.md) | Python that runs around dbt: extract, publish and CI fixtures. |
| [Tooling](tooling.md) | Development task entry points and environment lookup. |
