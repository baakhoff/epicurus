# Security Policy

epicurus is a self-hosted, local-first platform that handles personal data and
provider credentials, so security reports are taken seriously — even at this
pre-1.0 stage.

## Reporting a vulnerability

**Please don't open a public issue for a security vulnerability.** Report it
privately through GitHub's **[Security Advisories](https://github.com/baakhoff/epicurus/security/advisories/new)**
(the repo's **Security → Report a vulnerability**).

Include a description, reproduction steps, the affected component and version, and the
impact. You'll get an acknowledgement within a few days. This is a personal-scale
project, so fixes are best-effort — but security issues are prioritized over features.

## Supported versions

Only the **latest release** receives fixes. epicurus is on the `v0.x` line and under
active development; there are no long-term-support branches yet.

| Version | Supported |
| --- | --- |
| latest `v0.x` | ✅ |
| older | ❌ |

## Good to know

- Provider credentials and secrets live in **OpenBao**; the module↔core contract is
  **local-only** by default (ADR-0004 / ADR-0008), not exposed to the public internet.
- Known hardening work is tracked in the issue backlog (e.g. NATS auth #50, attachment
  upload limits #175).
