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
to GHCR:

```bash
git tag v0.2.0
git push origin v0.2.0
```

- Tags must be semver: `vMAJOR.MINOR.PATCH` (a `-suffix`, e.g. `v0.1.0-rc.1`, is
  published as a prerelease).
- Release notes are grouped by `type:*` label (Features, Fixes, Documentation,
  Maintenance), so good labels and Conventional Commit messages produce good
  notes.

## Changelog

Notable changes are recorded in
[`CHANGELOG.md`](https://github.com/baakhoff/epicurus/blob/main/CHANGELOG.md),
following [Keep a Changelog](https://keepachangelog.com/).
