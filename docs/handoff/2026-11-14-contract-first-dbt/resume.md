# Resume: contract-first dbt, end of session 2026-11-14

Supersedes `handoff.md` (stale: says A5e in progress). Read `design.md` and `plan.md` for intent; this file is state and next steps only.

## State

- This repo, `feature/TJP-14114`: 20 commits ahead of `origin/main` (v1.29.0), NONE pushed. Pushing a feature branch auto-merges and releases: push only a complete phase, owner says when.
- modelgen: `main` is v0.15.0 (PyPI, released). `feature/TJP-14115` has two unpushed commits (`ce102d0` `dbtColumnSetTest` customProperty and `tests_output` / `--tests-output`; `6ad1e5c` golden version swap). 434 tests pass. Verified end to end in the dbt sandbox against the real extract contract: 10 models, column-set test passes with `columns_match_set`, castable guard passes.
- ci-images `main` at `9c9dbad`. `ghost-runner:latest` is on `python:3.12-slim-bookworm` with `wraith-modelgen==0.15.0` baked; push goes via the LAN registry on `:3002` (the public domain 413s), now documented in its README.
- wraith plugin reinstalled from the local clone, v0.8.3. Restart Claude Code to load it.
- `GITEA_TOKEN` works (HTTP 200).

## Done this session (this repo)

B6: contract examples must match valueType patterns (`AtcoCode` loosened to `^[A-Za-z0-9]+$`); link `through` resolves; base types fit contract logicalType (`cast: true` marks deliberate conversions; new `DelaySecondsStat`); nullability agrees (contract is truth, primary keys exempt, 18 ontology bindings marked `nullable: true`); validator shrink. B5 part: `dbtSourceName: bods` on the extract contract; `dbt/tests/assert_extract_columns_castable.sql`. Left of B6: `test_metric_bound_to_missing_column_fails`, waits for B7.

## Next, in order

1. Owner decides: push modelgen `feature/TJP-14115` (merges and releases 0.16.0; owner then publishes to PyPI and says RELEASED 0.16.0) or use a post-generation script instead. Recommended: the push.
2. ci-images: bump the pin to `wraith-modelgen==0.16.0`, build under a local tag, verify (`python3 --version`; `import modelgen, sqlglot, duckdb`; `modelgen --help`; `uv pip check --system`), push via the LAN address, runner host re-pulls.
3. Finish B5: `.modelgen.yml` (`contracts: contracts/*.odcs.yaml`, `sources_contracts`, `sources_output: dbt/models/staging/bods`, `tests_output: dbt/tests`); add `dbtColumnSetTest: columns_match_set` to the extract contract; delete `_bods__sources.yml`; update `ops/dbt_sandbox.sh:90-95` (it patches `min_value: 500000` in that file and aborts without it: patch the generated `extract_row_count.sql` instead); fix stale `_bods__sources.yml` line refs in the extract contract; add `modelgen check` to CI; prove in the sandbox before committing. Do not run the generated output unverified again.
4. Docs fixes from the audit, small commits: `dbt/README.md:44-46` (RETENTION_DAYS vs extract_days are deliberately 3 and 4), `docs/bus-platform.md:61` (direction retired 2026-09-23), `docs/contracts.md` (six contracts, validation rules, policy gate, SLA checks), `docs/architecture.md:170-173`, `dbt/README.md` CONTRACT.md and "five tables", ADR-0010 status, `mkdocs.yml` nav (ADRs 0005-0009), journey_key vs Run wording. Then the stale `_bods__sources.yml` mentions (`dbt/README.md:477`, `docs/operations.md:219`, `docs/ROADMAP.md:194`, `generate_sandbox_positions.py:62`).
5. Ontology audit leftovers: `functions[].implementation` pointers are unchecked; 19 presentation fields are unread. Check whether `ghost-ontology/v1` requires them before cutting.
6. B4 remainder (sandbox `dbt compile` spike, then `compiled_sql_dir` in CI), then B7 to B14 per `plan.md`.
7. Architecture review report is at `/tmp/architecture-review-20261114.html` (may be gone): top pick was splitting the journey-key naming (Run key vs Journey key) with a glossary entry.

## Rules (unchanged)

Never touch `dbt/build.sh` (including wiring `ops/sla_check.publish`) until the final gate to main. No sudo, no host dbt (sandbox only), never read `.env` or print tokens. Commits `:gitmoji: feature/TJP-DDMMN: msg`, no attribution trailers. `.git/info/exclude` hides `AGENTS.md`, `docs/agents/`, `.scratch/`.
