# infra

Operational stacks and orchestration that are not application services:

- **[compose](compose/)** — the data-plane stack (Postgres, Valkey, NATS, Qdrant,
  OpenBao). ✅ available.
- **[edge](edge/)** — Traefik gateway routing services on one entry point;
  access-agnostic (the operator layers Tailscale / reverse proxy / Keycloak in
  front — ADR-0008). ✅ available.
- **[observability](observability/)** — Grafana / Loki / Prometheus / Tempo (OTLP
  straight into Tempo). ✅ available.
- **backup** — Restic configuration (encrypted, restore-from-anywhere).
- **vpn** — gluetun profiles for per-service VPN routing.

These land across Phase 0+.
