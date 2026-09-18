# ADR 0005: Migrate to dbt v2 and DuckDB 2.0 alpha, move `ghost-runner` off Alpine

**Status:** Reverted 2026-09-18 -- see [Rollback](#rollback-2026-09-18) below. The Debian base switch on `ghost-runner` stands; dbt v2 and DuckDB 2.0 alpha do not.
**Date:** 2026-09-17
**Related:** [docs/ROADMAP.md § 1.9.0](../ROADMAP.md), [ci-images](https://git.thomaspeoples.com/thomaspeoples/ci-images), `pyproject.toml`, `dbt/profiles.yml`

## Context

`ghost-runner` (the Gitea CI runner image, Alpine/musl) has never been
able to install `dbt-core` at all: `dbt-core-experimental-parser`, one of
its hard deps, ships no musllinux wheel and its build backend refuses to
compile from source on any platform without a matching prebuilt binary.
`dbt docs generate` has run on `ubuntu-latest` (glibc) instead since the
image existed, and dbt gating a CI build (ROADMAP 1.1.0) has been blocked
on this the whole time.

dbt Core v2.0 GA'd 2026-09-14, rebuilt on the Rust-based dbt Fusion
engine, and DuckDB 2.0 ("Cyanoptera") hit feature freeze the same week
with alpha builds available ahead of an October GA. Both were raised as
"Considered, not started" roadmap entries pending investigation.

## Investigation

**The musl blocker does not go away with dbt v2.** Checked directly, not
assumed: unpacked the `dbt` PyPI package's (2.0.4) sdist and found the
identical stub pattern as `dbt-core-experimental-parser` -- no real
source, a custom `_dbt_sa_build` backend that fetches a prebuilt wheel
from a fixed `assets.json` manifest. That manifest lists exactly
`macosx_10_12_x86_64`, `macosx_11_0_arm64`,
`manylinux_2_28_{aarch64,x86_64}`, `win_amd64` -- no musl target, same as
v1's. No dbt version was ever going to fix this on Alpine.

**`act_runner` itself is portable.** `ldd` against the binary inside
`gitea/act_runner:latest` reports "not a valid dynamic program" -- a
static Go binary with no musl/glibc dependency at all. Moving
`ghost-runner`'s base to Debian only required copying `act_runner` and
its `run.sh` wrapper out of the official image, not rebuilding anything.
Found for free while doing this: the existing image's
`ENTRYPOINT ["/bin/s6-svscan", "/etc/s6"]` pointed at a binary and a
config directory that exist nowhere in the base image -- confirmed by
inspecting it directly (no `s6-svscan` on `PATH`, no `/etc/s6`). Its real
entrypoint is `["/sbin/tini", "--", "run.sh"]`.

**dbt v2's native DuckDB adapter honors this project's tuned settings,
but not identically.** `dbt debug` confirmed `profiles.yml`'s
`settings:` block (`memory_limit`, `TimeZone`, `preserve_insertion_order`)
is read and applied. A full `dbt run` at the existing `memory_limit: 4GB`
OOM'd on 2 of 97 models (`int_pings_matched_to_stops`, `eta_models`) that
were stable at that limit under v1.12.4 + dbt-duckdb 1.11.0. Root-caused
before touching the number: the same two models, same day's data, same
host, re-run under v1.12.4 still passed cleanly at 4GB -- ruling out data
growth. dbt v2's bundled DuckDB engine is Fusion's own and entirely
independent of the `duckdb` PyPI package (confirmed: `dbt` v2 ran with no
`duckdb` package installed in the test venv at all), so this is a real
difference in the native adapter's memory overhead against the identical
setting, not a config regression or a data-volume artifact.

**dbt v2 parser is stricter about source config shape.**
`dbt parse --use-v2-parser` (run under the still-installed v1.12.4 first,
per the documented safe migration path) surfaced 4 errors: both
`_sources.yml` files had `meta`, `loaded_at_field` and `freshness` at the
old top level, which v2 rejects outright rather than warning. Fixed with
`dbt-autofix deprecations --all`, which also flipped
`require_generic_test_arguments_property: true` (this project's generic
tests already pass `arguments:` the new way) and dropped the
now-removed `target-path` key from `dbt_project.yml`.

**Everything downstream of dbt's own JSON artifacts still holds.**
`publish_run_results.py`'s assumptions about `run_results.json`
(`metadata.generated_at`, `results[].unique_id/status/failures/execution_time`)
were checked against a real v2-produced file -- schema unchanged.
`contracts/validate.py` and `publish_punctuality_summary.py` read the
built DuckDB file directly, never dbt's own artifacts, so they had no
exposure regardless. `packages.yml` (`dbt_expectations` 0.10.10)
resolved and ran clean under v2 with no version bump. No YAML anchors
exist in this project, so that breaking change doesn't apply either.

## Decision

Move `ghost-runner` to Debian, migrate `wraith-bus-ingestor` to
`dbt==2.0.4`, and pin `duckdb==2.0.0.dev2609121639` (`--pre`) -- ahead of
DuckDB's own GA. Raise `dbt/profiles.yml`'s `memory_limit` from 4GB to
6GB for the new engine.

The DuckDB alpha pin is a deliberate ahead-of-GA call, not an oversight:
DuckDB's own team calls alpha clients "explicitly not production-ready."
It was taken anyway because dbt v2's adapter doesn't use this package at
all (see above) -- the alpha pin's blast radius is limited to
`backend/app.py`, `ops/incident.py`, and `build.sh`'s own inline publish
step, not the dbt build path this ADR spent most of its investigation on.

The `memory_limit` bump is scoped to the new engine specifically, not a
repeat of the tuning history in `profiles.yml` (ADR-0001/0002 predate
this and remain about the old `dbt-duckdb` adapter). It was retested
clean: full `dbt run` (97 models, including all 86 `line_stops` per-line
batch models at `--threads 1`) + `dbt test` (884/884 pass) at 6GB, then a
real, un-`SKIP_EXTRACT`'d `build.sh` run against live QuestDB end to end.

## Consequences

**`dbt-duckdb` is no longer a dependency.** DuckDB support is native to
the `dbt` package under Fusion, and PyPI has no 2.x release of
`dbt-duckdb` regardless -- 1.11.0 is the ceiling, built against
dbt-core v1.x's adapter interface, not Fusion's. `uv.lock` dropped
~30 of dbt-core's own transitive Python deps (`pydantic`, `protobuf`,
`sqlglot`, `metricflow`, `networkx`, ...) that the Rust binary doesn't
carry.

**One cosmetic new warning, not a regression.** `dbt test` emits a
`SyntaxInvalid` warning on the existing `describe select * from (...)`
schema-drift macro (`dbt/macros/tests/schema_drift.sql`) -- Fusion's
static SQL linter doesn't fully parse that DuckDB construct even though
DuckDB executes it fine and the test passes. Same class of harmless
warning as the `type: SQL expression` one already dropped in 1.0.0; not
worth working around.

**ROADMAP 1.1.0 (gate dbt in CI) is unblocked, not closed.** A `dbt
build`/`dbt test` job can now run on `ghost-runner` itself instead of
requiring `ubuntu-latest`. Neither that job nor moving `pages.yml`'s
`dbt docs generate` off `ubuntu-latest` was done as part of this
migration -- both are real follow-ups, not implied by it.

**Revisit if:** DuckDB 2.0 GA lands and the alpha pin needs moving to a
real release; dbt ships a version that changes the bundled engine again
(retest `memory_limit` before assuming 6GB still holds -- it is now tied
to this specific engine, not a project-wide constant); or the
`describe select *` linter warning is promoted to an error in a future
Fusion release.

## Rollback (2026-09-18)

**GOTCHA: isolated verification (884/884 tests, 6GB `memory_limit`) did
not catch a real production memory blowup.** Every check above (`dbt
run` + `dbt test`, a real `SKIP_EXTRACT=0` `build.sh` run) passed clean.
It was only under `wraith_bus_build.service`'s actual nightly conditions
-- full 8-day extract, `--threads 1`, the 86-model `line_stops` batch
cluster back-to-back with the main run -- that the real ceiling showed
up, and it took three separate `MemoryMax` values (9000M, then 12000M as
an explicit measurement pass) and four real OOM-kills to pin down.

Timeline, same day:
1. **1st-3rd kills, `MemoryMax=9000M`:** each pinned at exactly 8.7G, the
   cap itself. First diagnosed as cgroup v2 counting page cache from
   `$BUILD` (the multi-GB `.duckdb` file) against the same ceiling as
   process memory -- fixed with a `posix_fadvise(DONTNEED)` cache drop in
   `build.sh` between the two `dbt run` phases. 2nd kill found a second
   cache source (`extract.py`'s freshly-written `data/*.parquet`, ~1.9GB)
   -- same fix extended to cover it. 3rd kill happened under genuinely
   clean conditions (swap at 0B, no other tenants, both cache fixes in
   place) -- ruled out contamination as the explanation.
2. **`MemoryMax` raised to 12000M** as a deliberate measurement pass, not
   a final number -- every prior attempt had been killed exactly at its
   own cap, so the true peak had never actually been observed.
3. **4th kill, `MemoryMax=12000M`:** got further (25/86 `line_stops`
   models, past every prior failure point) but still died. Root cause
   this time confirmed via the **kernel oom-killer log directly**
   (`journalctl -k`, `oom_memcg=/system.slice/wraith-bus-build.service`):
   `anon-rss:12244652kB` (~11.68GB) at kill, `file-rss:6784kB` -- real
   process memory, not page cache. `profiles.yml`'s configured
   `memory_limit: 6GB` was nowhere near honored by actual RSS.

**Conclusion:** dbt v2's bundled DuckDB 2.0 alpha engine either doesn't
correctly enforce `memory_limit` under this workload, or carries large
overhead outside its buffer manager (Fusion runtime, query compilation,
Arrow batch materialization across an 86-way model split) that
`memory_limit` was never meant to cap. Root cause not isolated further --
would need DuckDB's own memory profiling instrumentation mid-run to say
exactly where the gap goes, not attempted. The old stack
(`dbt-core==1.12.4` + `dbt-duckdb==1.11.0`) ran the identical workload
clean, repeatedly, under 9000M.

**Decision:** revert to `dbt-core==1.12.4` + `dbt-duckdb==1.11.0` and an
unpinned `duckdb` (see `pyproject.toml`), `profiles.yml`'s `memory_limit`
back to 4GB, `MemoryMax` back to 9000M. Keep the Debian base on
`ghost-runner` -- it fixed a real dead-entrypoint bug and the musl wheel
gap was never dbt-version-specific (v1's `dbt-core-experimental-parser`
has the identical stub-sdist pattern as v2's `dbt` package). Keep the
`posix_fadvise` cache-drop fix in `build.sh` -- harmless on the old
stack, cheap insurance if page-cache pressure ever recurs for an
unrelated reason.

**Revisit dbt v2 / DuckDB 2.0 only after:** DuckDB 2.0 reaches a real
GA with some production mileage elsewhere, and only with a rehearsal
plan that includes a real, full-scale, unattended `systemctl`-triggered
run under the actual nightly `MemoryMax` cap -- not just an isolated
`dbt run`/`dbt test` pass. That gap between "verified clean in
isolation" and "clean under real production load" is the actual lesson
here, independent of dbt/DuckDB version.
