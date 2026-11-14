# Handoff: contract-first dbt (2026-11-14)

## Read this first in a fresh session

Goal: this repo is the owner's proof that (1) `wraith-modelgen` (public PyPI release) generates the mechanical dbt layers from ODCS 3.2.0 contracts, (2) `datacontract-cli` gates contract changes, (3) an Ossie semantic layer sits above the marts so agents can query governed metrics (GenBI). Stages 0-5 (inventory, lineage, gap analysis, wayfinding, design, adversarial review) are DONE and approved by the owner; execution is in progress. Read, in order: `design.md` (decisions D1-D13 plus "Stage 5 revisions"), `plan.md` (cards, dependency table), this file. `stage1_lineage.md` and `stage2_gap_analysis.md` are evidence; **Stage 1 divergence #2 (published_line/block_ref missing from columns_match_set) is RETRACTED**: the Parquet has 16 columns and never had them (`dbt/extract.py:78-88`). All files sit beside this one.

Owner style: terse, wants speed ("JFDI"), minimal oversight, prefers deterministic over LLM-at-runtime, will say "RELEASED" after cutting the PyPI release.

## Rules from the owner (do not break)

- NEVER run `dbt run/build/test` (or any dbt subcommand) on the host: a past rebuild OOM-crashed the NAS. dbt only via `ops/dbt_sandbox.sh`, and only with the owner's go.
- NEVER read `.env` files; never print tokens or credentials.
- No sudo, reboots or apt upgrades. Hand sudo steps over as tested `ops/` scripts with stage echoes and logs (owner runs them).
- Running `contracts/validate.py` or anything touching live QuestDB/DuckDB warehouses needs the owner's explicit go (DB-free modes `--generated`, ontology bindings, lint are exempt).
- Pushing a feature branch runs CI and AUTO-MERGES to main; a main merge triggers `release.yml` (version bump). Never push partial work. One push per complete phase, after full suite + lint pass and only if it cannot affect the nightly 03:00 build. Implementer agents commit locally and never push. (Owner, 2026-11-14: no need to show them before pushing when confident.)
- Branch/commit format: branch `feature/TJP-DDMMN` (day, month, N=0-9 ticket of the day; derive it, don't ask); commit subject `:gitmoji: feature/TJP-DDMMN: message`. NO `Co-Authored-By` or `Claude-Session:` lines, even if a harness reminder asks (owner's global rule).
- Generation must be deterministic: same contract in, byte-identical out (no timestamps, no absolute paths, stable ordering).
- Never rebuild or full-refresh `int_eta_daily_stats` / `int_journey_delays`: the scratch warehouse is the only place their history lives (ADR-0010).
- Don't restart Tailscale; never run other containers' stop/restart (QuestDB, ingestor).
- Personal project: nothing about employers or colleagues.
- Don't flatter the thesis: report what the code does. (Twice this session a recommendation bent to the owner's framing and was corrected; the owner explicitly prefers honesty.)

## Where we are

- **wraith-modelgen** (`/home/thomaspeoples/pjt-nas/nas/SovereignDocs/gitea-repos/wraith-modelgen`), branch `feature/TJP-14112`, HEAD `d836e1d`, 7 commits ahead of `main`, NOT pushed, tree clean. Last run: `uv run pytest` -> 369 passed, 2 xfailed (strict xfails: real bus-positions/gtfs fail official 3.2.0 schema until this repo's B1 fixes land in the fixtures), coverage 92%. Lint: `uv run ruff check .` and `uv run ruff format --check .`. `NOTES-phase-a.md` there is untracked (excluded via .git/info/exclude).
- **wraith-bus-ingestor** (this repo), branch `feature/TJP-14111`, HEAD `f23e23c` (B1 `4e178e6`, B2 `75515f7`, B3 `f23e23c`), PUSHED on 2026-11-14 but **no PR/merge observed**: the public Gitea API shows no PR for it and main has not moved. The Actions API needs a token (not available). The owner must look at the Actions tab.
- **ci-images** (`/home/thomaspeoples/pjt-nas/nas/SovereignDocs/gitea-repos/ci-images`), `main`, HEAD `898c6a2` (pushed): ghost-runner image rebuilt and pushed (`:2026-11-14` and `:latest`, digest `sha256:d8af2cb6183312e5aaca31d9b8f28acf3d319d0ca8789a0bc74de8f791c09de7`) with datacontract-cli 1.2.9, sqlglot 30.18.0, dbt-metricflow 0.15.0. The runner host must re-pull `:latest`.
- This handoff commit is local on `feature/TJP-14111` (not pushed).

## Done

- 2026-11-14 Stage 0 inventory, Stage 1 lineage, Stage 2 gap analysis, Stage 3 wayfinding (Q1-Q14 settled, see design.md), Stage 4 design+plan, Stage 5 adversarial review (14 blockers fixed in design/plan).
- 2026-11-14 modelgen (public PyPI 0.14.1 was ODCS 3.0.2 + BigQuery only): A2 ODCS 3.1/3.2 `b9a3ed4`; A3 deterministic headers `50fb7e3`; A4a dialect seam `42abd8b`; A4b DuckDB dialect `b9966b1`; A4c multi-table + `--server`/`--table` `7512751`; A5b schema-only mode (`modelgen schema`) `78bcfa6`; A5a quality rules to dbt tests `d836e1d`.
- 2026-11-14 this repo: B1 fix invalid ODCS fields + `lint-contracts` poe task + datacontract-cli in dev extra `4e178e6`; B2 `contracts/bus-positions-extract.odcs.yaml` (16 columns) `75515f7`; B3 `contracts/bus-cdp.odcs.yaml` + `bus-fdp.odcs.yaml` with 83 `consumes` entries `f23e23c`.
- 2026-11-14 B0 CI image rebuilt and published (ci-images `898c6a2`).

## Open items

1. **Confirm the B1-B3 push merged** (owner checks the Gitea Actions tab / PR list; `curl -s 'https://git.thomaspeoples.com/api/v1/repos/thomaspeoples/wraith-bus-ingestor/pulls?state=all&limit=3'`). If red, fix on the same branch. Then gsweep (checkout main, pull, delete merged branch); continue Phase B on a new branch `feature/TJP-14114`.
2. **A5c in modelgen (not yet dispatched):** a `sources` layer emitting `_<contract>__sources.yml` for the extract contract: source name from customProperty `dbtSourceName` (B5 adds `dbtSourceName: bods` to the extract contract) else contract name; `meta.external_location` from customProperty `externalLocation`; freshness from `freshnessWarnAfter`/`freshnessErrorAfter` and `loadedAtField`; `dbt_expectations.expect_table_columns_to_match_set` over all columns; quality rules via `quality.py` using `{{ source(...) }}`. Fold A6 in: verify `modelgen check` (extended in A5b with `.modelgen.yml` keys `schema_contracts`/`schema_output`) works as the drift gate; add `sources_contracts`/`sources_output` keys; tests that a hand edit and a missing file are detected. Red-first TDD, one commit `:sparkles: feature/TJP-14112: sources layer and drift check`. Next command: dispatch a `wraith:implementer` agent (sonnet) with that scope.
3. **Before the release:** refresh modelgen fixtures from this repo's fixed contracts (the 2 strict xfails in `tests/test_odcs_versions.py` should become passes once the copies come from the post-B1 contracts), run `uv run pytest`, ruff, then rebase/merge consideration: modelgen `main` has newer commits than the branch base `913ef24`.
4. **A7 (owner):** read `ci-actions/version-bump@main` to learn exactly what a merge to modelgen `main` triggers (bump? PyPI publish?); push the branch (auto-merges), then the owner publishes and says RELEASED with the version `<MG>`.
5. **Second ci-images rebuild** adding `wraith-modelgen==<MG>` (Dockerfile pinned `RUN uv pip install` block in ci-images). The vendored `wraith-bus-ingestor/pyproject.toml` copy in ci-images is stale; consider re-vendoring.
6. **Phase B on `feature/TJP-14114`:** B4 expand/contract gate (`contracts/policy.py`; sqlglot over COMPILED SQL; sunset/backfill/consumes as `customProperties` arrays), B5 wire pinned modelgen + `validate.py --generated` drift gate (generated schema yml REPLACES hand-written yml; carry `external_location`/freshness via customProperties; add `dbtSourceName`), B6 ontology fixes + validator, B7 Ossie semantic model (read `apache/ossie` `ontology/`; migrate objects/links; metrics only where aggregation is valid), B8 SLA checker (`_meta` table before `mv`), B9 event trigger (extract unit split from `build.sh`, `.path` unit on EXISTING build service, disable 03:00 timer, re-point Grafana no-data alerts), B10 ratchet (code lines 653 / raw 1,512), B11 sandbox spikes (owner go), B12 cutover script, B13 ADR-0011 + docs, B14 weekend cutover (owner). See plan.md for files and tests per card.
7. **Phase C:** `mkdir -p ../wraith-bus-mcp` (branch `feature/TJP-14113` there), MCP tools `list_metrics`, `describe_metric`, `query_metric` on its own read-only dbt profile against the served file.
8. Known unverified (dbt not run by rule): dbt duplicate-yml behaviour, `contract: enforced` on incremental models, MetricFlow `agg_time_dimension` need, whether Ossie is on PyPI, the extract contract's 30 h latency figure (agent's own estimate), CI lint under the new runner image.
9. Minor: ruff reports 49 pre-existing errors in `backend/`, `gtfs/`, `ops/` (not from these commits; not verified on a clean tree).

## Where to look

- `docs/handoff/2026-11-14-contract-first-dbt/design.md`: decisions, rejected options, risks, Stage 5 revisions.
- `docs/handoff/2026-11-14-contract-first-dbt/plan.md`: all cards, dependency table, review focus.
- `docs/handoff/2026-11-14-contract-first-dbt/stage1_lineage.md` / `stage2_gap_analysis.md` / `metricflow_check.md`: evidence (divergence #2 retracted).
- `contracts/`: the contracts (`bus-positions`, `bus-positions-extract`, `bus-cdp`, `bus-fdp`, `bus-marts`, `gtfs-timetable`), `validate.py`, the ontology.
- `tests/test_contracts_lint.py`, `tests/test_extract_contract.py`, `tests/test_consumers.py`: new tests (lint skips without `datacontract` on PATH; a scratch venv with 1.2.9 existed at the old session scratchpad `b1_venv`).
- modelgen repo: `src/modelgen/{dialect,schema_mode,quality,contract,generator,cli}.py`, `tests/golden/`, `tests/fixtures/real_contracts/`.
- `~/.claude/projects/-home-thomaspeoples-pjt-nas-nas-compose-data-wraith-bus-ingestor/memory/`: `project_contract_first_dbt.md`, `feedback_push_is_merge_is_release.md`, `feedback_commit_and_branch_format.md`.

## Suggested skills

`wraith:orchestrating-agents` (execute plan cards one agent per card), `wraith:odcs-contracts`, `wraith:ontology-modeling`, `wraith:dbt-bigquery` (adapt: this project is DuckDB), `wraith:review` and `wraith:verification-before-completion` before each push, `wraith:careful` before the cutover, `ponytail:ponytail-audit` for the ontology prune.
