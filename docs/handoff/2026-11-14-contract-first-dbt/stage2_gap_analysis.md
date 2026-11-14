# Stage 2 gap analysis: does wraith-bus-ingestor generate its dbt from its contracts and ontology?

Date 2026-11-14. Repo: /home/thomaspeoples/pjt-nas/nas/compose-data/wraith-bus-ingestor (read-only; nothing in it was modified).
Builds on stage1_lineage.md (same dir). Abbreviations: BP/BM/GT = contracts/bus-positions, bus-marts, gtfs-timetable .odcs.yaml; ONT = contracts/wraith-bus-ingestor.ontology.yaml.

Short answer: **No, it does not.** Nothing in dbt is generated from a contract or from the ontology. The contracts act as an after-the-fact gate on dbt's output. Generation can honestly reach the source/schema YAML and the test layer. It cannot reach the SQL that matters. For this repo, the evidence favours "contract-driven tests and lint" over "contract-first model generation".

## Tools tested (installed from public PyPI only, per Thomas's instruction)

The local gitea-repos/wraith-modelgen checkout was NOT used for any finding. Early on, before the instruction changed, I read some of its source. Every claim below was re-derived from the PyPI install.

| package | version installed | venv |
|---|---|---|
| wraith-modelgen | **0.14.1** | scratchpad/venv_modelgen/.venv (uv, py3.12) |
| datacontract-cli | **1.2.9** (pulls open-data-contract-standard 3.2.0) | scratchpad/venv_dcc/.venv |

- Both tools ran only on copies: scratchpad/work/contracts, scratchpad/work/patched, scratchpad/work/down, and dbt project copies scratchpad/dbtcopy1, dbtcopy2, dcc_sync_bm_only.
- The dbt copies hold only dbt_project.yml, packages.yml, models/, tests/ and macros/. They hold no data, no duckdb file and no profiles.
- I never ran dbt, `datacontract test`, `datacontract dbt test` or validate.py. dbt is not installed in either venv.

## 1. Which way does the dependency run today? dbt first, contract second (as a gate)

Confidence: high.
- The contract is checked AFTER dbt builds. dbt/build.sh:256-257 runs `contracts/validate.py --marts dbt/$STAGED --contract BP --contract BM` against the candidate DuckDB. Only on success does build.sh:261 `mv` it into service. The contract therefore constrains dbt's output. It is not an input to dbt.
- No dbt file is generated. There is no "generated" or "do not edit" header and no `contract: enforced` anywhere (stage1 §0). There is no codegen step in build.sh.
- Prose cross-references run both ways, all written by hand:
  - The contracts cite the SQL. BM:46-49 names int_pings_segmented_to_journeys, int_eta_daily_stats and int_journey_delays. BM:156 says "see the `null_guards` comment in eta_models.sql".
  - The SQL cites the contracts. dbt/models/marts/eta_models.sql:98,168 and dbt/models/intermediate/int_journey_delays.sql:78.
  - BM's descriptions document SQL behaviour, so the SQL was written first and the contract describes it.
- History (stage 0): contracts and models were born in the same commit (4d9cfb1, 2026-09-12; `git log -- contracts/` and `-- dbt/models` both end there). Since then 13 commits touched models only and 9 touched contracts only. They co-evolve by hand, and the drift list (stage1 §C, 17 items) is the result.
- Structural gap (high confidence): **the object dbt actually reads has no contract.**
  - BP describes the QuestDB table (BP:49-60 server questdb-prod; BP pos_timestamp is "When WE wrote the row", ingest time).
  - dbt's source is extract.py's downsampled Parquet (_bods__sources.yml:19-21,31). There `timestamp` means "End of the 3-minute sample bucket, UTC" (:44), and the grain is one row per (bus_id, operator, line, 3-minute bucket) (:127-135).
  - So BP cannot generate the dbt source without generating something wrong.

## 2. What the contracts could drive deterministically, and the ceiling

Proven by running datacontract-cli 1.2.9 export twice on copies. **All `.out` files were byte-identical across runs** (`diff -r` shows only log timestamps; sha256 matched).

| dbt artefact | Generatable from contract? | Evidence | Conf |
|---|---|---|---|
| Source column list and columns_match_set | Yes, mechanically. It would fix divergence #3: published_line and block_ref are missing from _bods__sources.yml:123 | dcc dbt-sources on patched BP emits all 18 BP columns (out/bp1/dbt-sources.out) | high |
| not_null and accepted_values from required/enum | Yes | dcc output: BP direction accepted_values, not_null on required columns; BM 5-value enums including '*' (out/dcc1/bus-marts.dbt-models.out:36-86) | high |
| Column data_type plus `contract: enforced` | Yes for BM (DuckDB types VARCHAR/DOUBLE/BIGINT). No for BP: SYMBOL, LONG and FLOAT are QuestDB types. On a server of `type: custom` dcc falls back to a Snowflake mapping and warns "Cannot map type to snowflake SQL type ... SYMBOL" 11 times (out/bp1/dbt-models.log) | | high |
| Model-level SQL quality rules turned into singular tests | Yes for 9 BM rules (`dbt sync` wrote 9 `tests/datacontract_cli/...sql` with an "AUTO-GENERATED ... Do not edit" header). This would close stage1 §B gaps such as rowCount>500, r2 0..1, n_obs floor, and the global fallback with operator='*' and exactly 1 row (generated test: `metric_value <> 1`) | dbtcopy1/tests/datacontract_cli/ | high |
| Freshness | Not by either tool. dcc emits no freshness block (grep of outputs). BP's SLA (latency 10 m on recorded_at, BP:263-271) is the WRONG SLA for a nightly Parquet extract: generating it would error every run. The real dbt freshness (2d/5d on timestamp, _bods__sources.yml:38-41) has no contract home | | high |
| Descriptions/docs | Yes, but they would REPLACE the richer hand-written yml docs. `dbt sync` overwrote eta_models' 20-line description with BM's 2-line one (diff dbt/models/marts/_marts__models.yml vs dbtcopy1) | | high |
| Classification handling | Moot. No property in BP/BM/GT carries `classification` (grep `classification` hits only prose at BM:40). There is no personal data: bus_id is a vehicle | | high |
| Staging SQL | Only a bare `select col, col from source(...)`. dcc's dbt-staging-sql output is 3 lines, with no casts (out/bp1/dbt-staging-sql.out) | | high |

**Ceiling, from concrete files.** stg_bods__vehicle_pings.sql is 173 lines. Contract metadata cannot drive any of:
- a Jinja loop over optional columns that exist only in newer files (:54-56);
- the GB bbox and 0/0 filter (:69-73);
- a hard-coded bank-holiday exclusion (:96-98);
- explicit UTC and Europe/London conversion (:107-117);
- direction normalisation by `case lower(trim(...))` (:135-139);
- epoch-ms to timestamp (:164-168).

At most the column list and casts (about 10 lines) are generatable. eta_models.sql pools per-day sufficient statistics across 11 rungs using window sums and a `union all` loop (:158-196), handles NaN degeneracy (:204-208), and applies publish filters (n_obs>=200, slope>0, speed 3-90 km/h, :232-235). That is business logic: the contract can test it (and BM already states those rules), but it cannot produce it. The 5 intermediate models are private by contract (BM:7-10), so there is nothing to generate them from. **Realistic ceiling: the YAML (sources and schema tests) plus singular tests. About 0% of model SQL beyond trivial selects.** Confidence: high.

## 3. Ontology: what could drive dbt, and is it worth keeping for generation?

| ONT element | Could drive | Problem found | Conf |
|---|---|---|---|
| valueTypes enums (Direction, DayType, HourBand, ONT:62-79) | accepted_values | Already duplicated in BP/BM `enum` and in the yml. ONT's enums omit '*', so generating from ONT would produce WRONG tests for marts | high |
| AtcoCode pattern `^[0-9]{4}[A-Za-z0-9]+$` (ONT:43) | regex test | BP:200-208 documents origin_ref 'k28' as legitimately non-ATCO, so a generated test bound to origin_ref fails by design | high |
| primaryKey -> unique/combination tests | Only Locality [localityNorm] (ONT:367) and the two mart objects have dbt models | TravelTimeModel PK (ONT:397) omits operator vs BM's 5-column PK, so a generated test would be wrong. Conversely Vehicle PK [operatorRef, busId] (ONT:124) is MORE correct than BP's own PK (bus_id alone, BP:105-106), which contradicts BP's prose (BP:41-45). That fact belongs in the contract | high |
| linkTypes -> relationships tests | Only lineHasTravelTimeModel and lineHasPunctualityProfile (ONT:545-557) touch dbt models | They key on lineId alone, which ONT's own ServiceNumber valueType says is not unique. eta_models.line contains '*'. Line is backed by GT (built outside dbt). A generated relationships test would be false or unrunnable | high |
| functions, computed props, RagStatus, renderHints, Vehicle/Journey/Stop/LineStop objects | none (API/frontend semantics, not dbt) | documentation only | high |

**Verdict:** for generation, the ontology does not justify its cost.
- Its machine-usable parts (enums, PKs, patterns, link keys) are either duplicates of the contracts or wrong relative to them.
- ODCS 3.2.0 has native homes for those useful parts:
  - `relationships` (2 occurrences in dcc's bundled odcs-3.2.0.schema.json);
  - `authoritativeDefinitions` (10);
  - `semanticType` (already used in BM).
- datacontract-cli 1.2.9 maps ODCS `relationships` to field_relationships checks (datacontract/engines/checks/create_checks.py:658-724; datacontract/integration/dbt_test_mapping.py). I read this code but did not run it.

Recommendation: move the Vehicle composite key into BP, and move needed relationships into ODCS `relationships`. Keep ONT, if at all, as human documentation, and stop treating it as a generation input. Confidence: medium. This is a judgement call; the facts behind it are high.

## 4. Options compared

**(a) wraith-modelgen 0.14.1 as is: does not fit. Confidence: high (run evidence).**
- Bundles only the ODCS **v3.0.2** schema (site-packages/modelgen/schema/odcs-json-schema-v3.0.2.json; contract.py:50 `ODCS_API_VERSION = "v3.0.2"`).
- `modelgen validate` and `gen` reject all 3 repo contracts: "'v3.2.0' is not one of ['v3.0.2', ...]", with 21/32/56 schema errors. Most are 3.2-only keys such as property `id`, `semanticType` and `enum`.
- On a downgraded copy (scratchpad/work/down, scripts/downgrade.py) the next blockers were:
  - "Contract has no BigQuery server";
  - "must set modelgenLoadedAtField";
  - "casts to 'SYMBOL', which is not a BigQuery type" (14 lint errors).
- Its help says "BigQuery dbt model scaffolder", and it requires exactly one schema object per contract. BM has 3 and GT has 5.
- Only after rewriting the copy's types to STRING/INT64/FLOAT64 did it generate, and the output is BigQuery-shaped: `incremental_strategy='insert_overwrite'`, `partition_by={...'granularity':'day'}`, `ref('raw__bus_positions')`, and a `ds` snapshot column (out/mg4_1/stg__bus_positions.sql). I believe insert_overwrite is not a dbt-duckdb strategy, but I did not test it.
- Determinism holds: two runs were byte-identical (`diff -r out/mg4_1 out/mg4_2` empty). The header embeds the contract path as passed (`Contract: down/bp_step4.yaml`), so output is path-sensitive.

**(b) datacontract-cli 1.2.9: best fit for lint, breaking-change detection and test generation; poor fit as a model generator. Confidence: high.**
- Supports ODCS v3.2.0: lint check "Data contract is valid against ODCS v3.2.0".
- It found real defects in the repo contracts:
  - **BP (status: active) is invalid ODCS 3.2.0**: `dimension: validity` at BP:92 is not in the allowed enum.
  - **GT has YAML truncation bugs**: unquoted flow-mapping descriptions at GT:124 and GT:165 contain commas, so "(operator, line, direction)" and "not observed. Anchored..." are parsed as stray keys and the descriptions are cut short.
  - BM/GT `description.refresh` is an unknown field (BM:43, GT:45), which becomes an error in January 2027 per the tool.
  - validate.py checks apiVersion (validate.py:249-250) but never validates against the JSON schema, which is why these went unnoticed.
- `breaking` works offline. A BIGINT->VARCHAR change on a copy returned exit 1 with "ERROR Updated ... physicalType". Identical files returned exit 0.
- Export results:
  - deterministic;
  - source name is the contract UUID (`source('9f2c4b10-...','eta_models')`), which is unusable as-is;
  - dbt-models export emits `dbt_utils.unique_combination_of_columns`, but dbt/packages.yml has only metaplane/dbt_expectations;
  - staging SQL is a trivial select.
- `dbt sync` writes into an existing project:
  - it is deterministic (dbtcopy1 and dbtcopy2 are identical);
  - **it corrupted an unrelated file**: syncing BM alone rewrote dbt/models/staging/bods/_bods__sources.yml into invalid YAML ("while scanning a simple key"), de-indenting it although the help says "preserving comments and formatting";
  - it replaced the hand-written mart descriptions.
- So use sync only with `--dry-run`, or into a separate generated-tests dir after review. Never run it on the real repo without a git-diff gate.

**(c) Extend modelgen to read ontology bindings: not recommended. Confidence: medium.**
- Prerequisite work: ODCS 3.2 support, a DuckDB dialect, multi-object contracts, and dropping the BQ server/keystore/ds assumptions. That is effectively a new generator.
- The ontology input is the least trustworthy source (§3).

**(d) Hybrid: most defensible if anything is done. Confidence: medium.**
- datacontract-cli handles lint, breaking-change detection and singular-test generation (deterministic, ODCS 3.2 native).
- A tiny repo-local deterministic generator produces only the source column list and columns_match_set, plus not_null and accepted_values on marts. It must read a NEW contract for the extract.py Parquet, because BP is the wrong object.
- Model SQL stays hand-written.
- Even this rests on first fixing the 17 stage-1 divergences and the lint failures above.

## 5. Falsification: the strongest case that contract-first generation is NOT worth it here

1. Scale: 10 models. 2 staging models carry real logic, 5 intermediate are private (BM:7-10), and 3 marts are hand logic. The generatable surface is about 3 YAML files. The 17 divergences could be fixed by hand in less time than a generator takes to build.
2. The source of truth for dbt's input does not exist. BP is the QuestDB contract, not the Parquet contract. A generator would first need a new contract, which is more surface, not less.
3. The existing gate already protects consumers. build.sh:256-261 refuses to publish marts that break BM. Generation would move tests earlier but would not protect the API any better.
4. Generating documentation loses information. The hand-written yml descriptions are richer than the contract's, and dcc sync proved it overwrites them.
5. Tool risk is real. modelgen cannot read the contracts. dcc sync corrupted a YAML file. dcc export needs a package the repo lacks and emits UUID source names.
6. Team of one on a NAS. Every added tool is another venv, more CVE churn (modelgen's recent history is dependency-security bumps) and more build time. The owner wants FAST builds (memory: build time profile).
7. History shows the contract follows the SQL. BM cites eta_models.sql comments, so inverting the dependency means rewriting the contracts as specs first.

Counter-evidence that keeps a smaller version alive: the stage1 §B gaps (rowCount, r2 range, n_obs floor, operator='*' fallback) are closed for free by dcc-generated singular tests, and the lint already caught 3 real defects that validate.py misses.

## What I could not determine

- Whether the dcc-generated singular tests compile and pass under dbt-duckdb 1.11 (not run: no dbt).
- Whether `contract: enforced: true` plus dcc's data_type values (VARCHAR, DOUBLE, BIGINT) match the built tables' real DuckDB types (would need the DuckDB file).
- Whether dbt-duckdb rejects modelgen's `insert_overwrite` / `partition_by` config (belief only).
- Why dcc `dbt sync` of bus-marts touched _bods__sources.yml at all. I only observed that it does, reproducibly; root cause not traced in dbt_sync.py.
- Whether dcc relationships checks behave correctly on DuckDB (code read only, create_checks.py:658-724).
- The exact column schema of extract.py's Parquet output (dbt/extract.py not read in this stage).
- Whether any non-repo consumer relies on ONT (only validate.py reads it per stage 0).

## Artefacts (scratchpad)

- work/contracts (copies), work/patched (BP with validity->conformity; BM n_obs retyped), work/down (modelgen downgrade steps)
- out/dcc1, out/dcc2 (dcc exports, identical), out/bp1, out/bp2 (patched-BP exports, identical), out/mg4_1, out/mg4_2 (modelgen output, identical)
- dbtcopy1, dbtcopy2, dcc_sync_bm_only (dcc sync results; contain the corrupted _bods__sources.yml)
- scripts/odcs_errs.py, scripts/downgrade.py, scripts/yparse.py
