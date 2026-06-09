# Dual-track: Open-Source + SaaS

epicurus is built to fork into **two products from one codebase**:

1. **Open-source** — the repo goes public; anyone can self-host the full
   single-tenant (or self-managed multi-tenant) platform.
2. **SaaS** — we run the same core as a hosted, multi-tenant service with
   billing, signup, and hosted models.

These pull in partly opposite directions, so the preparation is structural and
**must hold from commit #1** — retrofitting it later is expensive and, for
tenant isolation, dangerous. This document is binding: weigh every change
against it.

## Model: open-core

```
   ┌─────────────────────────── OSS repo (public-ready) ───────────────────────────┐
   │  epicurus-core · agent · llm · knowledge · storage · memory · identity · web   │
   │  integrations/* · messaging · service-template · infra (compose, observ.)      │
   │  → complete, runnable, self-hostable on its own                                │
   └───────────────────────────────────────────────────────────────────────────────┘
                                      ▲  emits usage/events (NATS), exposes tenant ctx
   ┌──────────────────────── SaaS control-plane (private overlay) ──────────────────┐
   │  billing · metering · tenant provisioning · public signup/auth · plan limits   │
   │  → separate compose profile / private package; ABSENT from the OSS build        │
   └───────────────────────────────────────────────────────────────────────────────┘
```

The OSS build never imports the SaaS overlay. The SaaS build = OSS core + the
overlay. Core modules stay SaaS-agnostic: they expose a tenant context and emit
usage events; only the overlay knows about money, plans, or signup.

## Non-negotiables (apply to every change)

1. **Tenant is a first-class primitive — from Phase 0.** Every persisted row,
   NATS subject, Qdrant collection, OpenBao secret path, and object bucket is
   scoped by `tenant_id`, even while there is exactly one tenant. No code path
   may assume a single global tenant. `epicurus-core` provides and threads the
   tenant context; the service template wires it in by default.
2. **Services are stateless; state is externalized.** No service relies on local
   disk except disposable cache. State lives in Postgres / Redis(Valkey) /
   Qdrant / object storage. This is what makes both "one box" and "N replicas"
   work from the same code.
3. **Storage and LLM sit behind swappable backends.** Storage: local-FS
   (self-host over the HDD) ↔ S3/MinIO (SaaS), same interface. LLM: Ollama
   (local) ↔ hosted API, behind LiteLLM. Modules never hardcode a backend.
4. **Per-tenant credentials, never global.** Integration modules receive a
   tenant context and fetch *that tenant's* secrets from OpenBao. A module is
   stateless w.r.t. identity.
5. **SaaS-only concerns stay in the overlay.** Billing, metering, quotas,
   signup, plan enforcement live outside core modules. Core emits events;
   overlay consumes them.
6. **Secrets never in git.** gitignore + gitleaks (CI + pre-commit) + OpenBao.
   This is a precondition for going public at all.

## Licensing

**Our code** — decide before the repo goes public (tracked in ADR-0002):

- **AGPL-3.0 + CLA (recommended).** Network-copyleft deters competitors from
  re-hosting our code as a rival SaaS; the CLA preserves our right to
  dual-license our own commercial/SaaS build. Standard open-core posture.
- **Apache-2.0** — simpler, maximally permissive; better for adoption/community,
  but offers no moat against a competitor SaaS-ing the code.

**Dependencies** — redistribution & SaaS implications:

| Component | License | Note |
| --- | --- | --- |
| NATS, Qdrant, Prometheus, Traefik, LiteLLM, Ollama | Apache/MIT | Clean for OSS + SaaS |
| Postgres | PostgreSQL | Clean |
| OpenBao | MPL-2.0 | Clean; file-level copyleft only |
| Grafana, Loki, Tempo, SearXNG, MinIO | **AGPL-3.0** | Deploy **unmodified** alongside our code (mere aggregation) = fine. Do **not** fork them into our codebase. |
| Redis | **SSPL/RSAL** (since 7.4) | Anti-SaaS license. **Use Valkey (BSD) instead** to avoid the question entirely. |

## Pre-public / pre-SaaS checklists

**Before open-sourcing the repo:**
- [ ] License chosen and `LICENSE` added; CLA in place if AGPL.
- [ ] gitleaks clean across full history (not just HEAD).
- [ ] No tenant assumes-global code paths; `.env.example` complete; no creds.
- [ ] Self-host quickstart works from a clean clone.

**Before launching SaaS:**
- [ ] Tenant isolation verified (data, vectors, secrets, buckets, events).
- [ ] Public auth + signup (Authentik / provider), TLS, rate limits, abuse controls.
- [ ] Hosted-model routing + per-tenant quotas via LiteLLM.
- [ ] Metering/billing overlay; per-tenant export + delete (GDPR).
- [ ] Backups + restore tested per-tenant.
