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

### Pre-release checklist

Work through this before pushing the tag:

- [ ] **CHANGELOG audited** — `[Unreleased]` reconciled against `git log
      <last-tag>..HEAD` and the merged PR list, so every user-facing change has an
      entry and the section reads true against actual merged history. Rename
      `[Unreleased]` to the release version + date, and open a fresh empty
      `[Unreleased]` above it for what comes next.
- [ ] **Every merged PR since the last tag carries a `type:*` label** (`type:feat` /
      `type:fix` / `type:chore` / `type:docs` / `type:test`) — the GitHub Release's
      auto-generated notes group by this label, so a gap here means a missing or
      miscategorized line in the public release notes. *Known gap as of the v1.0.0
      prep: this convention lapsed early on and wasn't consistently applied — verify
      the actual label coverage before relying on this gate; don't assume it holds.*
- [ ] **After the workflow runs, verify every `ghcr.io/baakhoff/epicurus-*:{version}`
      tag actually exists** (one per `services/*/Dockerfile`) before announcing — a
      green workflow run doesn't guarantee every image published; check the package
      list itself.
- [ ] **Call out in the release notes that `:latest`-tracking operators auto-upgrade**
      — an operator with `EPICURUS_VERSION` unset in `.env` pulls `:latest` on their
      next reconcile cycle and lands on the new release with no explicit action.

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
