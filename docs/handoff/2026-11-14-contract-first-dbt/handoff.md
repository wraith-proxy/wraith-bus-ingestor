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

## Where we are (updated 2026-11-14, afternoon)

- **wraith-modelgen** (`/home/thomaspeoples/pjt-nas/nas/SovereignDocs/gitea-repos/wraith-modelgen`), branch `feature/TJP-14112`, NOT pushed, `origin/main` is still `913ef24` (nothing newer, so no rebase needed; merge dry-run clean). 10 commits through `e60b8cf` (A5c sources layer `cadc196`, A5d `9423319`, fixtures refreshed `e60b8cf`: 396 passed, 0 xfailed). A `wraith:reviewer` pass found two blockers, both being fixed by **A5e** (workflow `wf_8abaf5c4-e81`, one commit when done): (1) server selection regression (no `--server` must prefer the first `bigquery` server, as 0.14.1 did), (2) README/pyproject stale. A5e also restores the public Python API kwarg `contract_path` (accepted, ignored, `DeprecationWarning`), and hardens `_location`, scalar `customProperties`, zero freshness, header helper. Check `git log` in that repo: if A5e has not committed, its edits sit uncommitted in the tree. Known and accepted: generated header embeds the package version, so every release drifts `modelgen check`; BigQuery is only tested through mocks (no account).
- **wraith-bus-ingestor** (this repo): B1-B3 MERGED (PR #42, main v1.29.0). Branch `feature/TJP-14114` is 9 commits ahead of `origin/main`, NONE pushed: handoff docs (`3bf9778`), Gitea issue template (`c9e57c3`), archiver `RETENTION_DAYS` default 3 (`6918af6`), glossary + nav (`bc2204d`), B4 expand/contract gate `contracts/policy.py` + `tests/test_policy.py` (15 tests) (`265ee24`, `3ab6487`, `8bc526f`), `sqlglot` in dev extra (`ce36b62`), CI step running `policy.py` against `origin/main` (`336f2f8`). Do not push partial work; one push per complete phase.
- **ci-images**: runner image `:2026-11-14`/`:latest` (`sha256:d8af2cb6...`) has datacontract-cli 1.2.9, sqlglot 30.18.0, dbt-metricflow 0.15.0; the runner host has re-pulled it and `policy.py` was verified to run inside it (Python 3.11.2).
- **Local-only, hidden via `.git/info/exclude`** (never committed): `AGENTS.md` (Agent skills block), `docs/agents/`, `.scratch/`. `.scratch/stale-line-stops-names.md` is a drafted issue (remove stale `line_stops`/`line_localities` names); `GITEA_TOKEN` in the Claude shell returned 401, relaunch Claude Code from a shell with the fresh token to create it via API.
- **Domain vocabulary**: `docs/GLOSSARY.md` written (position report vs ping, journey vs run vs trip, delay, line/pattern, hot/extract/history windows, RDP/ODP/CDP/FDP with mart = FDP). Never grill or change these terms without reading it.

## Done

- Stages 0-5 (inventory, lineage, gap analysis, wayfinding, design, adversarial review).
- modelgen: A2 ODCS 3.1/3.2, A3 headers, A4a-c dialect seam, DuckDB, multi-table, A5b schema mode, A5a quality->tests, A5c sources layer, A5d fixes, fixture refresh. (A5e in progress.)
- This repo: B1-B3 merged; B4 gate done except the real `dbt compile` spike and wiring reader detection into CI.

## Open items

1. **A5e** lands (modelgen): review its commit and the three reviewer verdicts, then the OWNER does **A7**: read `ci-actions/version-bump@main`, push `feature/TJP-14112` (auto-merges), publish to PyPI, say RELEASED with `<MG>`.
2. **Second ci-images rebuild** adding `wraith-modelgen==<MG>`; re-vendor the stale `pyproject.toml` copy.
3. **Phase B on `feature/TJP-14114`** (continue): B4 remainder (spike: sqlglot over the 10 COMPILED models in the sandbox, owner go; then pass `compiled_sql_dir` in CI), B5 (needs `<MG>`: `.modelgen.yml` with `sources_contracts`/`sources_output`, add `dbtSourceName: bods` to the extract contract, delete hand-written `_bods__sources.yml`, fix stale line refs in that contract), B6, B7, B8, B9, B10, B11, B12, B13, B14. See plan.md.
4. **Phase C**: `../wraith-bus-mcp` (branch `feature/TJP-14113`), its own Gitea secrets are the owner's job, never the agent's.
5. Known unverified (dbt not run by rule): dbt duplicate-yml behaviour, `contract: enforced` on incremental models, MetricFlow `agg_time_dimension`, whether Ossie is on PyPI, the extract contract's 30 h latency figure.
6. Minor: ruff reports 49 pre-existing errors in `backend/`, `gtfs/`, `ops/`; `tests/test_extract_contract.py::test_extract_contract_lints_clean` fails when `datacontract` is not on PATH.

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
