# wraith-bus-ingestor

Live UK bus tracking data platform, extracted from `ghost-site` into its own
repo: a BODS SIRI-VM feed polled every 60 seconds, landed in QuestDB, turned
into DuckDB marts by a nightly dbt build, and served by a Flask API. The map
that consumes this API (`www/slaycoach/bus-map/`) lives in `ghost-site`,
which is purely the web front door; this repo owns everything from ingest
through to the API contract.

## What's here

| Piece | Content |
|---|---|
| `ingestor/` | Polls BODS, writes to QuestDB |
| `dbt/` | Staging → intermediate → marts, DuckDB-backed |
| `backend/` | The Flask API |
| `archiver/` | Moves aged-out partitions to cold storage, doesn't delete |
| `contracts/` | ODCS data contracts + a Foundry-style ontology |

## Where to look next

- **[Architecture](architecture.md)** — the compose services and how they're
  wired together, including the `ghost-net` seam back to `ghost-site`.
- **[Bus Platform](bus-platform.md)** — the ingest → QuestDB → dbt → DuckDB →
  Flask pipeline, and the modelling choices in it.
- **[Contracts](contracts.md)** — the ODCS data contracts and the ontology
  layered over them.
- **[Operations](operations.md)** — the nightly rebuild, retention, and how
  to force a from-scratch run.

## Source

`https://git.thomaspeoples.com/thomaspeoples/wraith-bus-ingestor`
