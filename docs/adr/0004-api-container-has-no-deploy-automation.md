# ADR 0004: `api` has no deploy automation -- merging to `main` does not ship code

**Status:** Accepted
**Date:** 2026-09-16
**Related:** [docs/ROADMAP.md § 1.5.0](../ROADMAP.md), [docs/operations.md](../operations.md)

## Context

1.5.0's Android app shipped with a dashboard screen backed by two new
Flask routes (`/metrics/summary`, `/metrics/punctuality`), added in
`backend/app.py` on `feature/TJP-16090`. That branch was reviewed, fully
tested (36 passing tests, verified against live QuestDB with real data),
merged to `main`, and released as `v1.2.0`/`v1.2.1` by the normal `cz`
flow.

The dashboard still showed "No data" on every field once installed on
the phone. `curl https://www.thomaspeoples.com/api/metrics/summary`
returned `404`. The route genuinely did not exist in the running
`ghost-backend` container -- because nothing rebuilds or restarts it.

## Investigation

All three `.gitea/workflows/*.yml` files were read end to end:

- `test.yml` runs the test suite and the contract validator on push/PR.
  It does not touch a running container.
- `pages.yml` ("Deploy Gitea Pages") builds and publishes documentation
  to a separate site. Despite the name, it deploys docs, not the API.
- `release.yml` ("Sovereign Release") runs `cz bump` and pushes a tag.
  It does not rebuild or restart anything either.

None of the three rebuild `ghost-backend`. Every other deploy this repo
has ever done (`dbt/build.sh` publishing new marts, `docker compose up -d
--force-recreate` after a config change) has been a manual, on-host
step, documented as such throughout `docs/operations.md` -- this is
consistent with that pattern, not a regression. The gap is that nothing
ever wrote that pattern down for `api` specifically, and nothing about
merging a PR gives any signal that a manual rebuild is still owed.

## Decision

Document the real deployment model rather than build automation for it
right now: merging to `main` makes code *available*, not *live*.
Shipping a backend change requires, by hand, on the host:

```bash
docker compose build api
docker compose up -d --force-recreate api
docker exec ghost-web nginx -s reload   # recreating changes the container's
                                         # IP; ghost-web cached the old one --
                                         # see docs/architecture.md
```

This is written up here and in `docs/operations.md` so the next person
(or the next session) checks whether a rebuild is owed before assuming a
merged PR is already serving traffic.

## Consequences

**Accepted gap, not fixed today.** A `deploy` job that rebuilds `api` on
push to `main` (mirroring `pages.yml`'s own push-triggered shape) is the
obvious real fix, but building it now would be solving a problem
observed exactly once under time pressure, not a validated need --
this stack has one deployer, on one host, who knows to run the commands
above once they're written down. Automating it is worth doing the next
time a backend change ships and someone forgets these three commands
again, not preemptively.

**Revisit if:** a second person starts deploying this stack, a backend
change ships without anyone present to run the manual steps, or `api`
gains a genuine multi-environment story (staging vs. production) where
"which container is running which commit" stops being answerable by
just looking at the host.
