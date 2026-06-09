# Deployment & CI/CD (target design — details deferred)

> **Status: planned, not yet built.** This captures the *intended* shape so the
> codebase grows compatible with it. The full how-to (writing the pipelines,
> wiring the server) is to be explained and implemented when we reach it — no
> action is required from this doc yet.

## Goal

Push to GitHub → automatically build and test → deploy to a **staging**
environment on the personal server → verify it's healthy → **promote to
production** (also on the personal server) **only if everything passes**.
A bad build must never reach production.

## Intended flow

```
  git push / PR ─► CI (GitHub Actions)
                     • lint (ruff) + types (mypy) + tests (pytest)
                     • gitleaks secret scan
                     • build & push versioned container images to a registry
                          │  (only on green)
                          ▼
                   Deploy to STAGING on the server
                     • same compose stack, isolated project/network/volumes
                     • run DB migrations
                          │
                          ▼
                   Smoke / health checks against staging
                     • /health on every service, key end-to-end checks
                          │  pass?
                ┌─────────┴─────────┐
              yes                   no ─► stop, alert, leave prod untouched
                │
                ▼
        Promote to PRODUCTION
          • same immutable images that passed staging (no rebuild)
          • blue-green or rolling swap; previous version kept for rollback
```

## Principles that keep us compatible (already in the architecture)

- **Immutable, versioned images** built once in CI and reused unchanged through
  staging → prod (never rebuilt per environment).
- **Config and secrets are external** (env + OpenBao), so the *same* image runs
  in staging and prod with different config. No secrets baked into images.
- **Staging and prod are isolated** (separate compose project names, networks,
  volumes, tenant data) on the same server, so testing can't touch prod data.
- **Stateless services + externalized state** make rolling/blue-green swaps and
  rollbacks safe.
- **Reversible promotion**: keep the last known-good image to roll back fast.

## Open choices (decide when we build this)

- CI provider (GitHub Actions assumed), container registry (GHCR vs self-hosted).
- How the server pulls/deploys: webhook + agent, GitHub self-hosted runner,
  or a pull-based tool (e.g. Watchtower-style / GitOps).
- Promotion trigger: automatic on green staging, or manual approval gate.
- Server access path (Tailscale-only deploy channel).

Owner has asked for a full walkthrough of this when the time comes; treat this
as the skeleton to flesh out then.
