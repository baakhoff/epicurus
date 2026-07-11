# Versioning

epicurus versions **per component** and ships the platform as a **bundled-stack
release**. The policy is fixed by ADR-0017; this page is the working reference.

epicurus follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Everything is `0.x` while the platform settles — see [The 0.x phase](#the-0x-phase).

## Two version axes

**1. Per-component version.** Every deployable unit owns its version in its own
`pyproject.toml` — `libs/epicurus-core` and each `services/*` (core-app, web,
echo, knowledge, storage, websearch, calendar, mail, tasks). Versions move
**independently**: a change to the mail module never forces a core-app bump.

**2. Bundled-stack version.** A repo **git tag** (`vMAJOR.MINOR.PATCH`) marks a
coherent release of the whole platform. Until the Phase-7 "add by domain"
installer makes modules individually installable, the stack ships as one bundle
(ADR-0012): a pushed tag triggers the [release workflow](releases.md), which
publishes a GitHub Release and pushes every service image to GHCR.

> The repo-root `pyproject.toml` stays at `0.0.0` on purpose: it is the
> uv-workspace aggregator, not a release unit. The stack version lives in the git
> tag, and the two axes are independent — the stack can be `v0.2.0` while a module
> that has not changed is still `0.1.0`.

## When to bump

The same scale applies to a single component and to the bundled-stack tag:

| Bump | Example | When |
| --- | --- | --- |
| **MAJOR** | `0.x → 1.0.0` | A brand-new / unseen capability, a rewrite, or something big. |
| **MINOR** | `0.1.0 → 0.2.0` | Changes how the **user interacts** with it — a new user-visible capability (core or module). |
| **PATCH** | `0.1.0 → 0.1.1` | Bug fixes or internal changes the user cannot see or validate. |

For the **stack tag**, read "the bundle" as the unit: a MINOR when a meaningful set
of new user-visible capability has shipped across the platform, a PATCH for a
fix-only roll-up. A new module joins the bundle at its own `0.1.0` and lifts the
stack by at least a MINOR — it is new user-visible capability.

### The 0.x phase

Every component is `0.x` today. Per SemVer, anything MAY change before `1.0.0`; we
still apply the table so each bump carries meaning. `1.0.0` is reserved for the
first deliberate "big" milestone (a MAJOR), not an automatic graduation from
`0.x`.

## Declare the bump on every change

**Every PR and every dispatch brief states its target bump**, per component, and
the reviewer enforces it:

- The PR template has a **Version bump** field — fill it in.
- The [dispatch-brief template](https://github.com/baakhoff/epicurus/blob/main/.github/ISSUE_TEMPLATE/dispatch-brief.md)
  carries a **Version bump** line, so the bump is decided when the work is scoped.

Examples:

- `mail MINOR` — adds a user-visible `mail_send` tool.
- `epicurus-core PATCH` — internal refactor, no contract change.
- `None — process/docs` — no shippable code changed (e.g. this policy PR).

[Conventional Commits](https://www.conventionalcommits.org/) (ADR-0003) feed an
eventual automated changelog.

## Cutting a release

See **[Releases](releases.md)** for the mechanics (tag → workflow → GHCR). Notable
changes are recorded in
[`CHANGELOG.md`](https://github.com/baakhoff/epicurus/blob/main/CHANGELOG.md),
following [Keep a Changelog](https://keepachangelog.com/).

## Module graduation at v1.0.0

Reaching the `v1.0.0` stack tag is a one-time exception to "modules freeze at their
own 1.0" above: instead of each component earning a MAJOR independently, a fixed set
**graduates together** at the stack tag (ADR-0017 amendment, 2026-07-11).

**Graduates to `1.0.0`:** `epicurus-core`, `core-app`, `web`, `storage`, `knowledge`,
`websearch`, `mail`, `notes`, `calendar`, `tasks` — every component that carried the
"foundation complete & stable" milestone.

**Stays `0.x`:** `messaging` (early Phase-4 work, still finding its shape) and `echo`
(a reference/example module, never meant to signal production-readiness).

After the tag, ordinary per-component SemVer resumes — a module's next MAJOR is its
own again. The graduation bump is its **own commit** (version fields only, no code
change bundled in), timed after every in-flight pre-1.0.0 PR has landed — an early
bump collides with their own version-line edits.
