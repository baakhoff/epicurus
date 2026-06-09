# infra

Operational stacks and orchestration that are not application services:

- **compose** — the `docker-compose` files that bring up the platform
  (Traefik, Postgres, Valkey, NATS, Qdrant, OpenBao) and supporting stacks.
- **observability** — Grafana / Loki / Prometheus / Tempo + the OTel collector.
- **backup** — Restic configuration (encrypted, restore-from-anywhere).
- **vpn** — gluetun profiles for per-service VPN routing.

These land across Phase 0+ — see [docs/ROADMAP.md](../docs/ROADMAP.md).
