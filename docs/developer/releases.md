# Releases

epicurus follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The first release is **v0.1.0** — the first version usable on a server with a UI.
Until then the platform is under construction and there is nothing to release.

## Cutting a release

A release is a pushed git tag. The `Release` workflow publishes a GitHub Release
with notes generated from the merged pull requests:

```bash
git tag v0.1.0
git push origin v0.1.0
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
