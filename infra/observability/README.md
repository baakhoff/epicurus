# Observability

The observability stack — a compose fragment assembled into the top-level stack
(ADR-0006). Brought up with the rest via
`docker compose up -d` / `task up`.

| Service | Image | Role |
| --- | --- | --- |
| Prometheus | `prom/prometheus` | Scrapes services' `/metrics` (every service exposes them via `epicurus-core`). |
| Loki | `grafana/loki` | Log store. |
| Alloy | `grafana/alloy` | Ships Docker container logs → Loki. |
| Tempo | `grafana/tempo` | Trace store; receives OTLP directly (gRPC/HTTP). |
| Grafana | `grafana/grafana` | UI, with Prometheus / Loki / Tempo datasources pre-provisioned. |

## Use

Open **Grafana** at <http://localhost:3000> (anonymous admin in local dev). The
three datasources are pre-wired:

- **Metrics** flow today (Prometheus scrapes `echo:8080/metrics`, etc.).
- **Logs** flow today (Alloy → Loki; explore them in Grafana).
- **Traces** infrastructure is ready — Tempo receives OTLP directly; services
  start emitting spans when OpenTelemetry tracing is wired into `epicurus-core`
  (a follow-up).

Send OTLP traces to Tempo at `tempo:4317` (gRPC) / `tempo:4318` (HTTP) on the
internal network (also published on the host). A dedicated OTel collector (for
batching / fan-out) can be added later — the contrib/core collector images don't
run on this Docker Desktop, and Tempo's built-in receiver covers the need for now.
