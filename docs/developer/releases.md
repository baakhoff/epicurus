# Releases

epicurus follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
full policy — per-component versions plus the bundled-stack tag — is in
**[Versioning](versioning.md)** (ADR-0017).

A **release** is the bundled stack: one git tag ships the whole platform (core, web,
and every module) as a single unit, until the Phase-7 installer makes modules
individually installable (ADR-0012). **v0.1.0** was the first release (the core
runtime); each later tag is cut when a meaningful set of changes has shipped.

## Cutting a release

A release is a pushed git tag. The `Release` workflow publishes a GitHub Release
with notes generated from the merged pull requests, and pushes every service image
to GHCR — both a versioned tag (`:<semver>`) and the mutable `:latest` alias:

```bash
git tag v0.2.0
git push origin v0.2.0
```

- Tags must be semver: `vMAJOR.MINOR.PATCH` (a `-suffix`, e.g. `v0.1.0-rc.1`, is
  published as a prerelease).
- Release notes are grouped by `type:*` label (Features, Fixes, Documentation,
  Maintenance), so good labels and Conventional Commit messages produce good
  notes.

## Deploying a specific release

All service compose fragments use `${EPICURUS_VERSION:-latest}` for the image
tag. Set `EPICURUS_VERSION` in `.env` (see `.env.example`) to the semver you are
deploying — without the leading `v`:

```env
EPICURUS_VERSION=0.2.0
```

Omitting the variable (the default) resolves to `:latest`, which is fine for
local development where you always want the freshest build. For staging and
production, always set it — this ensures every `docker compose up` pulls the same
immutable image that was verified in CI, satisfying the immutable-image principle
described in **[Auto-deploy (CD)](../infrastructure/auto-deploy.md)** (same image
through staging → prod, no surprise updates from a bad push to `:latest`).

## Automatic deployment to the operator's box

Once a tag is pushed the box reconciles itself — no manual `docker compose pull`
needed.  Two mechanisms are available (scheduled script or Watchtower); the full
walkthrough, GHCR authentication, and rollback procedure are in
**[Auto-deploy (CD)](../infrastructure/auto-deploy.md)**.

## Changelog

Notable changes are recorded in
[`CHANGELOG.md`](https://github.com/baakhoff/epicurus/blob/main/CHANGELOG.md),
following [Keep a Changelog](https://keepachangelog.com/).
