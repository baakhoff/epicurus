# infra

Operational stacks and orchestration that are not application services:

- **[compose](compose/)** — the data-plane stack (Postgres, Valkey, NATS, Qdrant,
  OpenBao). ✅ available.
- **edge** — gateway + private ingress. Paired and added later (the gateway has
  nothing to route until app services exist).
- **[observability](observability/)** — Grafana / Loki / Prometheus / Tempo (OTLP
  straight into Tempo). ✅ available.
- **backup** — Restic configuration (encrypted, restore-from-anywhere).
- **vpn** — gluetun profiles for per-service VPN routing.

These land across Phase 0+.
