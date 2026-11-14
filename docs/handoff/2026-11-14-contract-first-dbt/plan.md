# Contract-first dbt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use wraith:orchestrating-agents to execute this plan card by card.

**Goal:** Prove modelgen + datacontract-cli + an Ossie semantic layer on wraith-bus-ingestor: generated mechanical dbt layers, a contract gate, and agent-queryable metrics.

**Architecture:** ODCS contracts at every layer boundary drive generated RAW/STAGING files and schema/tests via the public `wraith-modelgen` release; `datacontract-cli` lints and checks breaking changes; hand-written SQL is limited and ratcheted; Ossie metrics compile to MetricFlow YAML served by a separate read-only MCP project.

**Tech Stack:** Python 3.12, uv, dbt-core 1.12.4, dbt-duckdb 1.11.0, DuckDB 1.5.5, ODCS v3.2.0, datacontract-cli 1.2.9, wraith-modelgen (>0.14.1, to be released), dbt-metricflow 0.15.0, sqlglot, Apache Ossie spec (pinned SHA), pytest.

**Spec:** `design.md` in this directory (decisions D1-D13).

## Global Constraints

- `contracts/validate.py` modes that touch no database (`--generated`, `--ontology` bindings, lint) are exempt from the owner-go rule; every mode that opens QuestDB or DuckDB still needs the go.
- Determinism tests run under two `PYTHONHASHSEED` values and `LC_ALL=C`.
- Phase 1 rules persist: NEVER run dbt run/build/test on the host; dbt only via `ops/dbt_sandbox.sh`. NEVER read `.env` files. No reboots, no apt upgrades, no sudo (hand over sudo steps as `ops/` scripts with stage echoes and logs; owner runs them).
- Anything touching live QuestDB or the DuckDB warehouses needs the owner's explicit go first, including `contracts/validate.py` runs.
- Generation is deterministic: same contract in, byte-identical out. No timestamps, no absolute paths, sorted keys, stable ordering.
- Tools: public PyPI releases only, versions pinned in `pyproject.toml`/`uv.lock`. Never `datacontract dbt sync`.
- Commits: `:gitmoji: feature/TJP-14111: message`; plain messages, no co-author and no session-link lines. Ticket number `<n>` is supplied by the owner per phase.
- Hand-written SQL baseline: 1,512 raw lines / 653 code lines (10 models, comments and blanks stripped); the count may not rise without an explicit baseline bump in the PR.
- Never rebuild `int_eta_daily_stats` / `int_journey_delays` (only place history lives, ADR-0010). Never full-refresh to apply a contract change.
- Models: sonnet for implementation and review; haiku for mechanical edits; opus only for orchestration and hard synthesis. Effort is stated per card.

## Review Focus

1. Contract change that renames a column read by hand-written SQL: must be blocked in CI with the reader named (pinned by B4 test).
2. Extract that validates against its schema but has `timestamp` semantics changed: schema passes; only quality rules catch it (pinned by B2 rule tests; known residual).
3. Two runs of the generator on different checkout paths: must be byte-identical (pinned by A3).
4. Incremental history table when a column is added: must be altered in place, not rebuilt (pinned by B11(b) and the B12 rehearsal).
5. Metric bound to a column that the contract later deprecates/removes: validator fails (pinned by B6).

---

## Dependency table

| Card | Needs |
|---|---|
| A1-A6 | A1 first, then A2-A6 in order |
| A7 | A1-A6 |
| B0 | nothing (human) |
| B1, B2, B3 | nothing |
| B4 | B3 (+ CI sandbox compile from B0/test.yml) |
| B5 | A7 (`<MG>`), B0, B2, B3 |
| B6 | B5 |
| B7 | B5, B6 |
| B8 | B2 |
| B9 | B2, B8 |
| B10 | nothing |
| B11 | owner go |
| B12 | B5, B9, B11 |
| B13 | B12 |
| B14 | all of B |
| C1-C2 | B7 |
| C3 | B7, B8, B11 |
| C4 | C3, B14 |

`_generated/` ownership: A-generated files (sources, raw, schema yml) are modelgen's; `_semantic.yml` is B7's compiler's. B5's drift gate checks only files whose header names modelgen.

---

## Phase A: wraith-modelgen (repo /home/thomaspeoples/pjt-nas/nas/SovereignDocs/gitea-repos/wraith-modelgen, branch `feature/TJP-14112`; owner cuts the PyPI release)

### Card A1: Survey and baseline
**Model/effort:** sonnet, low. **Files:** Create `NOTES-phase-a.md` in the modelgen repo (not committed). **Produces:** the module paths, CLI entry point and existing golden-test layout, listed in NOTES, that A2-A6 consume.
- [ ] Locate: ODCS version check, type mapping, materialisation emitter, header emitter, CLI entry; run its existing test suite; record pass count.
- [ ] Expected: suite green on `main`; NOTES lists the five locations with file:line. **Commit:** none.

### Card A2: Accept ODCS 3.0.2 through 3.2.0
**Model/effort:** sonnet, medium. **Files:** the version-check module found in A1; Test `tests/test_odcs_versions.py`.
**Produces:** `SUPPORTED_ODCS_VERSIONS` containing `v3.0.2`, `v3.1.0`, `v3.2.0`.
- [ ] Failing test `test_accepts_v3_2_0_contracts`: copies of the repo's three contracts (`bus-marts`, `bus-positions`, `gtfs-timetable`) parse without "is not one of" error.
- [ ] Failing test `test_rejects_unknown_version`: `v9.9.9` raises with the supported list in the message.
- [ ] Implement; run; **Commit:** `:sparkles: feature/TJP-14112: accept ODCS v3.1 and v3.2 contracts`.

### Card A3: Deterministic output
**Model/effort:** sonnet, medium. **Files:** header emitter, YAML writer; Test `tests/test_determinism.py`. **Produces:** header `# GENERATED by wraith-modelgen <version> from <contract-name> <contract-version>. Do not edit.`; no path, no timestamp.
- [ ] Failing test `test_output_byte_identical_across_runs` (run under `PYTHONHASHSEED=0` and `=1`, `LC_ALL=C`) and `test_output_independent_of_checkout_path` (same contract copied to two dirs, `diff -r` empty).
- [ ] Failing test `test_header_contains_no_timestamp_or_absolute_path` (regex `\d{4}-\d{2}-\d{2}T` and leading `/` absent).
- [ ] Implement sorted keys and stable ordering; **Commit:** `:white_check_mark: feature/TJP-14112: byte-identical output, no paths or timestamps in headers`.

### Card A4: DuckDB target
**Model/effort:** sonnet, high. **Files:** type mapper, materialisation emitter; Test `tests/test_duckdb_target.py` with golden `tests/golden/duckdb/`. **Consumes:** A1 locations. **Produces:** `target: duckdb` selected from `servers[].type == duckdb`; logicalType/physicalType mapped (`FLOAT`, `DOUBLE`, `BIGINT`, `VARCHAR`, `TIMESTAMP`); materialisations limited to view/table; no `partition_by`, no `insert_overwrite`, no `ds` column.
- [ ] Failing test `test_bus_marts_generates_duckdb_models` against golden; `test_no_bigquery_constructs` (grep output for `insert_overwrite|partition_by|FLOAT64`).
- [ ] Implement; **Commit:** `:sparkles: feature/TJP-14112: DuckDB target driven by servers.type`.

### Card A5: Sources, raw models and schema yml with enforced contracts and rule tests
**Model/effort:** sonnet, high. **Files:** generators for `_<src>__sources.yml`, `stg_<src>__<table>_raw.sql`, `_<layer>__models.yml`; Test `tests/test_schema_generation.py`. **Produces:** `contract: {enforced: true}` with data types; tests from quality rules (`rowCount` to `dbt_expectations`-style or singular tests, `unique`, `not_null`, `accepted_values`, library rules to SQL singular tests); `columns_match_set` from the schema.
- [ ] Failing tests: `test_sources_columns_match_set_lists_every_contract_column` (the 16 extract columns; NOTE Stage 1 divergence #2 was WRONG: `published_line` and `block_ref` are ingest-contract columns the Parquet never held, `dbt/extract.py:78-88`), `test_eta_models_global_fallback_rule_includes_operator_star`, `test_enforced_contract_has_types`.
- [ ] Implement; **Commit:** `:sparkles: feature/TJP-14112: generate sources, raw models, enforced schema yml and rule tests`.

### Card A6: `--check` mode
**Model/effort:** sonnet, low. **Files:** CLI; Test `tests/test_check_mode.py`. **Produces:** `wraith-modelgen <contract> --out DIR --check` exits 1 and prints the differing paths if DIR differs from a fresh generation, 0 if identical.
- [ ] Failing tests `test_check_exits_zero_when_identical`, `test_check_exits_one_when_hand_edited`. Implement; **Commit:** `:sparkles: feature/TJP-14112: --check mode for drift detection`.

### Card A7: Release (human)
Owner runs the suite, bumps the version with their release process, publishes to PyPI. Output of this card: the released version string `<MG>`. **Phase B cards B4 onward wait for `<MG>`.**

---

## Phase B: wraith-bus-ingestor (branch per card group; one weekend cutover)

### Card B0: Rebuild the CI image (human)
CI installs nothing (`.gitea/workflows/test.yml:16-25`; deps are baked into ghost-runner via ci-images). Owner rebuilds ci-images with `datacontract-cli==1.2.9`, `sqlglot`, `wraith-modelgen==<MG>` (after A7), `dbt-metricflow==0.15.0`. B1 CI steps cannot pass before the first rebuild.

### Card B1: Fix the contracts and lint them in CI
**Model/effort:** sonnet, medium. **Files:** Modify `contracts/bus-positions.odcs.yaml:92` (invalid `dimension: validity`), `contracts/gtfs-timetable.odcs.yaml:124,165` (quote commas), `bus-marts.odcs.yaml` and `gtfs-timetable.odcs.yaml` (`description.refresh` to a valid field), `.gitea/workflows/test.yml`, `.pre-commit-config.yaml`; add `datacontract-cli==1.2.9` to the dev extra. Test: `datacontract lint contracts/<each>.odcs.yaml` exits 0.
- [ ] Run `datacontract lint` on each: expect the three defects. Fix; re-run: exit 0. Add CI step and pre-commit hook. **Commit:** `:bug: feature/TJP-14111: fix invalid ODCS fields and lint contracts in CI`.

### Card B2: `bus-positions-extract` contract
**Model/effort:** sonnet, high. **Files:** Read `dbt/extract.py` and `_bods__sources.yml` first; Create `contracts/bus-positions-extract.odcs.yaml`; Test `contracts/tests/test_extract_contract.py` (pytest, parses YAML only).
**Produces:** server type local Parquet; `timestamp` documented as bucket-end 3-minute; `recorded_at` null boundary 2026-09-12; `slaProperties` landing time; quality rules incl. row count >= 500k and lat/lon bounds.
- [ ] Failing test `test_extract_contract_lists_every_column_extract_writes` (column set taken by importing `KEYS`/`WANTED`/`TYPES` from `dbt/extract.py`; the SELECT is built at runtime, `extract.py:107-114`; `recorded_at` null boundary is 2026-09-20, `_bods__sources.yml:112`; the extract has 16 columns, NOT published_line/block_ref). Write; lint; **Commit:** `:sparkles: feature/TJP-14111: contract for the extract Parquet`.

### Card B3: CDP and FDP contracts with consumer declarations
**Model/effort:** sonnet, high. **Files:** Create `contracts/bus-cdp.odcs.yaml`, `contracts/bus-fdp.odcs.yaml`; each table carries `customProperties` as an array `[{property: consumes, value: "<contract>.<table>.<column>"}, ...]` (shape as `bus-positions.odcs.yaml:301`). Test `contracts/tests/test_consumers.py`: every consumed column exists upstream. **Interfaces produces:** `consumes` property format used by B4/B5.
- [ ] Failing test; write contracts from `stg_*`/`int_*` outputs (names/types read from the model SQL); lint; **Commit:** `:sparkles: feature/TJP-14111: contracts for staging and presentation layers`.

### Card B4: Breaking-change gate with expand/contract policy
**Model/effort:** sonnet, high. **Files:** Create `contracts/policy.py` and `contracts/tests/test_policy.py`; CI step. **Produces:** `check_change(base: Path, head: Path) -> list[Violation]`; violations: breaking diff without major bump; removal before `sunset` date; removal with detected readers (sqlglot over COMPILED SQL from `dbt compile` in the CI sandbox, not the Jinja source); expand step without `backfill` in {`derive`,`null-before`,`replay`}. `sunset`, `backfill` and `consumes` are `customProperties` array entries.
- [ ] Failing tests: `test_rename_read_by_hand_sql_is_blocked_naming_the_reader`, `test_additive_with_backfill_passes`, `test_additive_without_backfill_fails`, `test_removal_after_sunset_with_zero_readers_passes`.
- [ ] Spike first: confirm sqlglot parses the 10 COMPILED models (target/compiled) without error; also test `select *` and the `ping_columns` expansion resolve to named columns. Add `test_select_star_reader_is_detected`. **Commit:** `:sparkles: feature/TJP-14111: expand/contract gate`.

### Card B5: Generation wiring and the drift gate
**Model/effort:** sonnet, high. **Consumes:** `<MG>`. **Files:** Modify `pyproject.toml` (pin `wraith-modelgen==<MG>`), Create `ops/generate.sh`, Modify `contracts/validate.py` (new `--generated` check), `.gitea/workflows/test.yml`, `dbt/build.sh`. Test `contracts/tests/test_generated_equals_regen.py`.
**Produces:** generated files under `dbt/models/**/_generated/` that REPLACE the hand-written `_bods__sources.yml`, `_bods__models.yml`, `_naptan__*.yml`, `_intermediate__models.yml` and `_marts__models.yml` (delete them; one yml per model). Hand-only settings (`external_location`, `freshness`, expectations tests) are carried as contract `customProperties` and emitted by the generator (add `test_sources_keep_external_location_and_freshness`); `validate.py --generated` exits 1 if a fresh regeneration differs.
- [ ] Failing test `test_hand_edit_to_generated_file_fails_gate`. Implement; regenerate; **Commit:** `:sparkles: feature/TJP-14111: validate.py gates generated output equal to a fresh regeneration`.

### Card B6: Ontology fixes and stronger validation
**Model/effort:** sonnet, high. **Files:** Modify `contracts/wraith-bus-ingestor.ontology.yaml` (enums add `*`; AtcoCode pattern allows `k28`; TravelTimeModel key adds `operator`; link keys fixed), `contracts/bus-positions.odcs.yaml` (Vehicle key `[operatorRef, busId]`, relationships), `contracts/validate.py:334-394`; Test `contracts/tests/test_ontology_binding.py`.
- [ ] Failing tests: types and nullability parity, pk parity, link `through`, `test_metric_bound_to_missing_column_fails`. Run `ponytail:ponytail-audit` on the ontology; cut elements with no metric and no agent use. **Commit:** `:bug: feature/TJP-14111: fix ontology defects, check bindings to the contracts`.

### Card B7: Ossie semantic model: metrics, objects and links (spike then build)
**Model/effort:** sonnet, high. **Files:** Create `contracts/metrics.ossie.yaml` (pinned spec SHA in header), `ops/compile_metrics.py`, `dbt/models/marts/_generated/_semantic.yml`; Test `contracts/tests/test_metrics_compile.py`.
**Produces:** `compile(ossie_path: Path) -> str` returning MetricFlow YAML; metrics limited to what aggregates validly: journeys observed (sum of `n_journeys`), `n_stops`, locality counts. NOT p90 (not aggregable, `line_punctuality.sql:26`), NOT sums over `eta_models` including `'*'` rollup rows (`eta_models.sql:31-38`); a metric over `eta_models` requires the filter `operator != '*'`. The marts have no time column: add the `agg_time_dimension` spike result or publish a per-journey delay table (decision recorded in the card). Test `test_no_metric_aggregates_a_percentile`.
- [ ] Read `apache/ossie` `ontology/` first and migrate the object and link types of `contracts/wraith-bus-ingestor.ontology.yaml` into the Ossie ontology format (pinned SHA); retire the Foundry-style file once `validate.py` bindings pass against the new one (B6 retargets accordingly).
- [ ] Spike: run `ossie-converters` dbt converter (read-only) twice on a small model; if output is not byte-identical or targets a different shape, write the compiler. Test `test_compile_is_deterministic`. **Commit:** `:sparkles: feature/TJP-14111: Ossie semantic model and metrics compiled to MetricFlow YAML`.

### Card B8: SLA checker and data-as-of
**Model/effort:** sonnet, medium. **Files:** Create `ops/sla_check.py`, `ops/tests/test_sla_check.py`. **Produces:** `data_as_of(path: Path) -> datetime` and `check_slas(contract: Path, now: datetime) -> list[Breach]` reading `slaProperties`; writes a `_meta(data_as_of, contract_version)` table INTO the staged warehouse before the atomic `mv` (a sidecar file could disagree with the served file).
- [ ] Failing tests `test_breach_when_latency_exceeded`, `test_meta_table_written_before_publish`. **Commit:** `:sparkles: feature/TJP-14111: check contract SLAs, publish data-as-of`.

### Card B9: Extract unit, readiness marker and the build path unit
**Model/effort:** sonnet, high. **Files:** Modify `dbt/build.sh` (run with `SKIP_EXTRACT=1` under the unit); Create `ops/ready_gate.py`, `ops/systemd/wraith-bus-extract.service` (runs `extract.py` then `ready_gate.py`), `ops/systemd/wraith-bus-build.path` (`PathExists=` the marker, triggering the EXISTING `wraith-bus-build.service`, which keeps `MemoryMax=13000M`, its window and failure hook), `ops/install_event_trigger.sh` (sudo steps for the owner: install units, disable the 03:00 timer in the same script, stage echoes, log), edits to `ops/grafana/provisioning/alerting/rules.yaml` (re-point the `noDataState: Alerting` rules at lines 50, 94, 189 so waiting on the producer does not page; add a 'waiting' state row and a bounded wait time limit), `docs/operations.md`; Test `ops/tests/test_ready_gate.py`.
**Produces:** the build starts only on the marker; `ExecStartPre` consumes the marker so a failing build cannot retrigger; `ready_gate.validate(extract_dir: Path, contract: Path, since: date) -> bool` checks the whole 4-day window or only the new day (decision recorded in the card); a failed validation never writes the marker. Page conditions: API unable to serve only; own-contract breach is a ticket.
- [ ] Failing tests: `test_invalid_extract_does_not_write_marker`, `test_valid_extract_writes_marker`, `test_marker_is_consumed_before_build`, `test_timer_and_path_not_both_enabled` (reads the install script). Sudo install is run by the owner. **Commit:** `:sparkles: feature/TJP-14111: split extract, event-triggered build, no racing timer`.

### Card B10: Hand-SQL ratchet
**Model/effort:** haiku, low. **Files:** Create `ops/sql_ratchet.py`, `ops/sql_baseline.txt` (1512), CI step; Test `ops/tests/test_ratchet.py`. **Produces:** exit 1 if non-generated model SQL CODE lines (comments and blanks stripped; 653 today, raw 1,512) exceed the baseline; includes `stg_naptan__stops.sql`; the baseline file records both numbers.
- [ ] Failing test `test_fails_above_baseline`. **Commit:** `:sparkles: feature/TJP-14111: ratchet on hand-written SQL`.

### Card B11: Sandbox spikes (owner go required)
**Model/effort:** sonnet, medium. Via `ops/dbt_sandbox.sh` only, never the host build. Record in `docs/adr/spikes.md`: (a) `contract: enforced` on view/table/incremental on dbt-duckdb 1.11 fails a deliberate column mismatch; (b) `on_schema_change` appends a column to an incremental table; (c) `mf query` peak memory on a sandbox COPY of `$BUILD` (real data volume, not a synthetic warehouse), via a separate profile on the served file opened `read_only`.
- [ ] Expected: (a) build error naming the column; (b) column added, history rows intact; (c) peak RSS number. If (a) fails, B12 falls back to generated column tests. **Commit:** `:memo: feature/TJP-14111: record sandbox spike results`.

### Card B12: Cutover rehearsal and runbook
**Model/effort:** sonnet, high. **Files:** Create `ops/cutover.sh` (stage echoes, logs, `--dry-run`, df and memory preflight), `docs/runbooks/cutover.md`; Test `ops/tests/test_cutover_dryrun.py` (pytest wrapper so it is collected). Stages: backup `$BUILD`; make CANDIDATE a copy of `$BUILD` and BASELINE a second copy; build the old code into BASELINE and the new code into CANDIDATE, both `SKIP_EXTRACT=1` on the same `data/`; compare keys and row counts exactly and DOUBLE columns with tolerance for the 3 marts; run `validate.py` (needs owner go) and `datacontract lint` on the candidate; atomic `mv`; keep the old file. History tables ride in the `$BUILD` copy, never rebuilt. Rollback is lossless only within EXTRACT_DAYS=4.
- [ ] Dry run in the sandbox: all stages echo; the compare stage fails on a seeded key difference and passes on a seeded 1e-9 double difference. **Commit:** `:sparkles: feature/TJP-14111: cutover script with rehearsal and rollback`.

### Card B13: Docs and ADR-0011
**Model/effort:** haiku, low. Facts inline from `design.md`. **Files:** Create `docs/adr/0011-contract-first-dbt.md`; Modify `docs/contracts.md`, `dbt/README.md` (fix the "no packages.yml" claim at `dbt/README.md:116` and "five marts" at `dbt/README.md:65`, the `contracts/README.md` reference at ontology line 8). **Commit:** `:books: feature/TJP-14111: ADR-0011 and contract docs`.

### Card B14: Weekend cutover (human)
Owner runs `ops/cutover.sh` per runbook; three nightly builds on the ready event must pass with zero pages; record the SQL ratchet baseline.

---

## Phase C: wraith-bus-mcp (new project at `../wraith-bus-mcp`)

### Card C1: Scaffold
**Model/effort:** haiku, low. `mkdir -p ../wraith-bus-mcp`; `uv init`; Create `pyproject.toml`, `README.md`, `tests/`. **Commit** (in the new repo): `:tada: feature/TJP-14113: scaffold wraith-bus-mcp`.

### Card C2: Read-only tools
**Model/effort:** sonnet, medium. **Files:** `src/wraith_bus_mcp/server.py`; Test `tests/test_tools.py`. **Produces:** `list_metrics() -> list[MetricSummary]`, `describe_metric(name: str) -> MetricDetail` (definition, grain, bound column, contract version), reading the compiled Ossie model.
- [ ] Failing tests `test_list_metrics_returns_names_and_meanings`, `test_describe_unknown_metric_errors`. **Commit.**

### Card C3: `query_metric`
**Model/effort:** sonnet, high. **Consumes:** B7 MetricFlow YAML, B8 `data_as_of.json`, B11(c) memory result. **Produces:** `query_metric(metric: str, dimensions: list[str], filters: dict, limit: int) -> QueryResult` with `data_as_of` and `contract_version`; DuckDB opened `read_only`, `memory_limit` from B11(c), no free SQL.
- [ ] Failing tests: `test_result_carries_data_as_of`, `test_unknown_dimension_rejected`, `test_no_sql_parameter_exists`. **Commit.**

### Card C4: End-to-end demonstration
**Model/effort:** sonnet, medium. Owner runs an agent against the server; transcript saved to `docs/demo/punctuality.md` in the MCP repo: question "how punctual is line X at the morning peak?" answered only via `query_metric`, traced Ossie metric to contract column to data, with data-as-of shown.
- [ ] Expected: answer cites the metric definition and carries data-as-of. **Commit.**

---

## Self-review

- **Spec coverage:** D1 A1-A7; D2 B2; D3 B3; D4/D10 B6, B7; D5 A5, B10; D6 A3, A6, B5 (the required drift-gate card); D7/D8 B1, B4; D9 B8, B9; D11 C1-C4; D12 B12, B14; D13 B14, C4, B13.
- **Not covered by a test, by design:** silent upstream semantic drift (known residual, Review Focus 2).
- **Dependency order:** A1-A6 before A7; B1-B3 and B6-B8, B10, B11 are independent of `<MG>`; B5 needs `<MG>`; B12 needs B5, B9, B11; B14 needs everything; C3 needs B7, B8, B11.
- **Ticket numbers and the `<MG>` version string are supplied by the owner;** no other placeholders.
