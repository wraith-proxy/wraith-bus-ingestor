## v1.25.0 (2026-10-26)

## v1.24.0 (2026-10-26)

## v1.23.0 (2026-10-26)

## v1.22.0 (2026-10-26)

## v1.21.0 (2026-10-26)

## v1.20.0 (2026-10-12)

## v1.19.0 (2026-09-25)

## v1.18.0 (2026-09-25)

## v1.17.0 (2026-09-25)

## v1.16.0 (2026-09-25)

## v1.15.0 (2026-09-24)

## v1.14.0 (2026-09-24)

## v1.13.0 (2026-09-24)

## v1.12.0 (2026-09-22)

## v1.11.0 (2026-09-21)

## v1.10.0 (2026-09-21)

## v1.9.0 (2026-09-20)

## v1.8.0 (2026-09-19)

## v1.7.0 (2026-09-18)

## v1.6.0 (2026-09-18)

## v1.5.0 (2026-09-17)

### Bug Fixes

- split the Android app into its own repo

## v1.4.1 (2026-09-17)

### Bug Fixes

- visible route lines, reordered bus-tap flow, line/bus search, nav dead-end

## v1.4.0 (2026-09-16)

## v1.3.0 (2026-09-16)

## v1.2.1 (2026-09-16)

## v1.2.0 (2026-09-16)

## v1.1.0 (2026-09-16)

## v1.0.1 (2026-09-15)

## v1.0.0 (2026-09-14)

### Features

- own the data -- migrate maps/, questdb_data/, osrm_data/ in from ghost-site

### Bug Fixes

- correct stale comment -- validate.py runs under this repo's own venv, not ghost-site's
- shrink SSD retention to 8 days, DRY dbt's own window, ETA fallback for every bus
- silence dbt macro arg-type warning, correct stale roadmap backoff note

## v0.2.2 (2026-09-14)

### Bug Fixes

- document the dbt-docs publish workflow in README
- generate dbt docs locally against real data, commit them -- CI skips regen if already present
- bootstrap empty source fixtures for docs-only dbt builds -- pages.yml never runs extract.py, so the parquet/csv globs matched zero files

## v0.2.1 (2026-09-14)

### Bug Fixes

- sync explicit extras, not --all-extras -- dbt still can't build on musl
- restore contract-validation step and --all-extras -- duckdb baked into ghost-runner now
- drop PyPI/CLI badges and roadmap milestone -- not a package candidate

## v0.2.0 (2026-09-13)

### Bug Fixes

- tool.uv package = false, not a hatch wheel-packages hack
- dbt/README.md and profiles.yml still referenced the old separate .venv path
- unify dbt onto the project's own uv venv, not a separate one

### Features

- adopt wraith house style, own docker-compose.yml
- full dbt structure-guide compliance, coordinated rename
- wire the contract gate into build.sh and CI
- adopt wraith house style, delete meal-picker, pin deps
