# Contract-first dbt for wraith-bus-ingestor: design

Date 2026-11-14. Status: REVISED after Stage 5 review (14 blockers, see 'Stage 5 revisions' at the end). Evidence: scratchpad/stage1_lineage.md, stage2_gap_analysis.md, metricflow_check.md.

## Purpose

The repo is a proof of three claims by its owner:
1. `wraith-modelgen` (public PyPI release) generates the dbt layers that are mechanical, from unmodified ODCS 3.2.0 contracts.
2. `datacontract-cli` can gate contract changes (lint, breaking) alongside it.
3. An ontology/semantic layer (Ossie) above the marts defines metrics that agents query (GenBI) through a governed interface.

Finding that motivates the work (Stage 1/2): today nothing in dbt is generated; the contract follows the SQL (`dbt/build.sh:256-261`), 17 divergences exist, intermediates have no contract, and the Parquet that dbt actually reads has no contract.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Fix modelgen first (Phase A, own repo/branch); repo pins the public release. Contracts are not downgraded. | The proof is only worth something if the released tool eats the real contracts. Public 0.14.1 knows only ODCS v3.0.2 and emits BigQuery shapes (Stage 2). |
| D2 | New contract `bus-positions-extract` for the Parquet landing files. | dbt reads extract.py's downsampled Parquet, not QuestDB; `timestamp` means bucket-end (`_bods__sources.yml:44`). |
| D3 | Layers RAW(RDP) / STAGING(CDP) / PRES(FDP) / FFDP(marts). One contract per data product, one table per model. Each consumer declares what it reads. | Contract at every boundary so an upstream change is checkable downstream. |
| D4 | Contracts are the only input to dbt generation. Ontology owns meaning and metrics, bound to contract columns. | Stage 2: ontology is a poor dbt-generation input (enum `*`, AtcoCode `k28`, TravelTimeModel key). |
| D5 | Generated: sources yml, `stg_*_raw`, schema yml with `contract: enforced`, types, quality-rule tests. Hand-written: `stg_bods__vehicle_pings.sql`, `stg_naptan__stops.sql`, 5 intermediates, 3 marts. Generated schema/sources REPLACE the hand-written yml for those models (no two yml files for one model); hand-only settings (`external_location`, `freshness`, expectations tests, `_bods__sources.yml:30-41`) are carried as contract `customProperties` and emitted by the generator. Ratchet counts code lines (comments and blanks stripped: 653 today; raw count 1,512) and the baseline file records both. | Staging logic, segmentation, OLS fit can't be generated (Stage 2). `transformLogic` rejected: relocates the spanner, unverified. |
| D6 | Generated files: fixed header (generator, pinned version, contract+version; no timestamps, repo-relative paths) in `_generated/` dirs. CI and `validate.py` regenerate to a temp dir and require zero diff. | Determinism and drift detection. |
| D7 | Policy: expand/contract. No one-step breaking change. Old+new coexist to a sunset date. Every expand declares `derive`, `null-before` or `replay` backfill. History tables never full-refreshed. Reader detection by sqlglot over COMPILED SQL (`dbt compile` in the CI sandbox), because the hand SQL is Jinja plus `select *` plus `ping_columns` `run_query`; sunset/backfill/consumes are ODCS `customProperties` arrays of `{property, value}` (shape as `bus-positions.odcs.yaml:301`), since ODCS has only `deprecated: boolean`. | Wildfire-free upstream change. |
| D8 | CI: `datacontract lint` + `breaking`. Never `datacontract dbt sync` (corrupted `_bods__sources.yml` in Stage 2). | Static, cheap, caught 3 real defects. |
| D9 | Event-triggered build in two units: an extract unit (split out of `build.sh:106-111`) lands and contract-validates the Parquet; a `.path` unit on the EXISTING `wraith-bus-build.service` (keeps MemoryMax 13000M, window, failure hook, `Before=` ordering) fires on the ready marker, which `ExecStartPre` consumes so a failing build cannot re-trigger all day. The 03:00 timer is disabled in the same handover script. `build.sh` runs with `SKIP_EXTRACT=1`. A bounded wait writes a 'waiting' state row; the Grafana no-data alerts (`ops/grafana/provisioning/alerting/rules.yaml:50,94,189`, `noDataState: Alerting`) are re-pointed so waiting on the producer does not page. Pages: API unable to serve only; own-contract breach is a ticket. data-as-of is a `_meta` table written into the staged file before the `mv`. | Producer/consumer ownership, without racing builds or contradicting the existing alerts. |
| D10 | Ontology: metrics authored as an Ossie semantic model (spec pinned by commit SHA; 0.3.0.dev0 is draft). Compile to MetricFlow YAML. Fix ontology defects; move Vehicle key + relationships into contracts; extend `validate.py`. Objects and links ALSO adopt Ossie's ontology spec (owner decision 2026-11-14); the existing Foundry-style ontology is migrated, not kept. B7 begins by reading `apache/ossie` `ontology/` (ontology.md, ontology.json, mapping.json). | Open standards; the draft spec is the main risk, hence pin. |
| D11 | MCP server is a separate project `../wraith-bus-mcp`: `list_metrics`, `describe_metric`, `query_metric` via MetricFlow, read-only, capped memory, no free SQL in v1. It uses its OWN dbt profile/target on the served file opened `read_only` and reopened per query (the build `mv`-swaps the file, `build.sh:265`); it never uses the repo profile, which targets the build warehouse and would take the lock the nightly needs. Metrics are defined only where aggregation is valid (no p90-of-p90s, `line_punctuality.sql:26`; `eta_models` `'*'` rollup rows excluded, `eta_models.sql:31-38`); the marts have no time column. | Governed metrics; keeps this repo about data. |
| D12 | Single weekend cutover: modelgen released beforehand; the CANDIDATE is a copy of the persistent `$BUILD` warehouse (it holds the only 8-day history, `build.sh:29-38,179-188`; `data/` holds 4 days, `extract.py:258-267`) rebuilt with `SKIP_EXTRACT=1`; the BASELINE is the old code on a second copy of the same `$BUILD` and `data/`. Compare keys and row counts exactly, DOUBLE columns with a tolerance (3-thread sums, `profiles.yml:23`). Atomic `mv`; old file kept; df and memory preflight; rollback is lossless only within EXTRACT_DAYS=4. | Owner's choice; the repo's publish mechanism (`dbt/README.md:62-79`) limits failure to "no swap". |
| D13 | Done = three demonstrations (generate+diff zero; breaking PR blocked, additive PR propagates; agent answers via Ossie metric) + weekend cutover done + 3 green event-triggered nights with zero pages + ratchet baseline + ADR-0011. | |

## Rejected options

- Bend contracts to modelgen 0.14.1 (downgrade to 3.0.2): proves less.
- `datacontract dbt sync` as generator: corrupted unrelated file; BigQuery/dbt_utils shapes.
- Ontology as dbt-generation input (option c): least trustworthy input, new generator on top.
- `transformLogic` in contracts for PRES logic (A): relocates hand SQL into YAML, unverified; (C) declarative segmentation primitives: research project.
- Strangler migration over several weeks: replaced by the weekend cutover (owner's choice).
- Free-form text-to-SQL for agents in v1.

## Risks (for Stage 5)

1. Ossie spec is draft/mutable; converters unverified. Mitigation: SHA pin, spike, fallback compiler.
2. `contract: enforced` on dbt-duckdb 1.11 SQL path is unverified (ADR-0001 notes a silent no-op on the Python path).
3. History tables (`int_eta_daily_stats`, `int_journey_delays`) hold the only history (ADR-0010). Copy, never rebuild; backup first.
4. `mf`/MCP memory on the NAS is unmeasured (OOM history ADR-0001/0009).
5. Silent semantic drift upstream passes schema validation. Mitigated only by quality rules.
6. Modelgen release is on the critical path and is a human step.
7. Determinism leaks: dict ordering, absolute paths, timestamps, locale-dependent sort.
8. Extract Parquet schema not read yet (card B1 reads `dbt/extract.py` first).

## Not in scope

BigQuery-shaped modelgen output, free-form text-to-SQL, TfL data, the QuestDB side, alert routing tooling beyond defining the two page conditions.

## Stage 5 revisions (what changed and why)

B1 cutover history loss: D12 rewritten (candidate = copy of `$BUILD`). B2 EXCEPT on doubles: tolerance. B3 and B4: D9 rewritten (existing unit, timer disabled, extract unit, marker consumed, waiting row, alert re-pointed). B6 generated vs hand yml clash: D5 overlay. B7 sqlglot/Jinja: D7 compiled SQL. B8 ODCS shapes: D7. B9 mf lock: D11. B10 bad metrics: D11. B11 CI image: card B0. B12 dependencies: table in plan. B13 validate.py go: exempt DB-free modes. B14 data-as-of: `_meta` table before `mv`.
Added risks: `mf` memory measured on a synthetic warehouse tells us little (B11(c) uses a copy of `$BUILD`); determinism tests run under two `PYTHONHASHSEED` values with `LC_ALL=C`; `dbt/README.md:65` says "five marts", there are three.
Still unverified (dbt not run by rule): dbt duplicate-yml error, `contract: enforced` on incremental, MetricFlow `agg_time_dimension` need, whether Ossie is on PyPI.

## Owner decisions after Stage 5 (2026-11-14)
Approved Stage 2 report and this design; minimal oversight from here. Branches: Phase A modelgen `feature/TJP-14112`, Phase B this repo `feature/TJP-14111` (already checked out), Phase C `feature/TJP-14113` (format TJP-DDMMN). modelgen repo path: /home/thomaspeoples/pjt-nas/nas/SovereignDocs/gitea-repos/wraith-modelgen. Human steps: A7 PyPI release (owner says RELEASED), B9 sudo install, B11/B12 live-data go, B14 weekend run (Saturday ok), C4 demo. Ossie ontology spec adopted for objects and links.
