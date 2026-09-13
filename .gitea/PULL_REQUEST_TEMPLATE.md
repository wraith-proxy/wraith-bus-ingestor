# 👻 wraith-bus-ingestor Pull Request

## 🎯 Purpose
_What are we haunting today? Briefly describe the change._

**Fixes:** # (Link the Gitea Issue here)

---

## 🛠️ Proposed Changes
- [ ] Logic updated in `backend/`, `dbt/`, `ingestor/` or `archiver/`
- [ ] Dependencies synced via `uv lock`

---

## 🚦 Quality Gate
- [ ] **uv Sync:** Environment is healthy and `uv.lock` is up to date.
- [ ] **Tests:** `./bin/run_tests.sh` passes with coverage at or above the gate in `pyproject.toml`.
- [ ] **Linting:** `pre-commit run --all-files` passes.
- [ ] **Commits:** Messages follow the `cz_customize` regex (Fix/Feature/Break).
- [ ] **Contracts:** `uv run python contracts/validate.py` passes against live data, if this touches `dbt/` or `contracts/`.

---

## 🧪 Deployment & Verification
- [ ] Branch is synced with the latest `main`.
- [ ] If this changes a dbt model or the marts schema, `cd dbt && ./build.sh` was run and published clean.

---

## 👤 Author's Final Word
_Any specific notes for the reviewer or warnings about breaking changes?_
