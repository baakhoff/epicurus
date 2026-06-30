# Observability

The observability stack — a compose fragment assembled into the top-level stack
(ADR-0006). **Opt-in:** every service here is gated behind the `observability` compose
profile, so a plain `docker compose up -d` / `task up` runs a lean stack *without* it.

```sh
docker compose --profile observability up -d   # with Grafana/Prometheus/Loki/Tempo
docker compose up -d                            # without (the default)
```

Nothing in epicurus depends on this stack at runtime — every service exposes `/metrics` and
`/health` regardless — so if you'd rather use `docker logs` or point your **own**
Prometheus/Grafana at those endpoints, you can leave the profile off entirely.

| Service | Image | Role |
| --- | --- | --- |
| Prometheus | `prom/prometheus` | Scrapes services' `/metrics`, discovered from Docker by the `epicurus.metrics.port` container label. Evaluates alert rules. |
| Alertmanager | `prom/alertmanager` | Receives firing alerts from Prometheus and routes to notification channels. |
| Blackbox exporter | `prom/blackbox-exporter` | HTTP probe for OpenBao's `/v1/sys/health` — fires the `OpenBaoSealed` alert when the vault is sealed. |
| Node exporter | `prom/node-exporter` | Host-level disk, CPU, and memory metrics from the WSL2 VM where Docker stores volumes. Powers the `DiskSpaceHigh` alert. |
| Loki | `grafana/loki` | Log store. |
| Alloy | `grafana/alloy` | Ships **epicurus** containers' logs → Loki, labelled by `service_name` + `container`. |
| Tempo | `grafana/tempo` | Trace store; receives OTLP directly (gRPC/HTTP). |
| Grafana | `grafana/grafana` | UI, with Prometheus / Loki / Tempo / Alertmanager datasources pre-provisioned. |

## Use

Open **Grafana** at <http://localhost:3000> (anonymous admin in local dev). The
datasources are pre-wired:

- **Metrics** flow today — Prometheus discovers modules from Docker by the
  `epicurus.metrics.port` container label (set in each module's compose
  fragment; the service-template includes it) and scrapes their `/metrics`.
- **Logs** flow today (Alloy → Loki; explore them in Grafana). Alloy ships logs
  only for containers in the `epicurus` compose project and labels each stream with
  `service_name` (the compose service) and `container`, so unrelated containers on
  the host are ignored and you can filter by service.
- **Traces** flow today (#57) when enabled — every service emits OpenTelemetry spans
  to Tempo over OTLP/HTTP, covering FastAPI requests and the NATS event bus (trace
  context propagates across the bus, so one trace spans publisher → handler). Opt-in
  like this whole stack: set `OTEL_TRACES_ENABLED=true` and bring it up with the
  `observability` profile. Wiring + config: the
  [tracing reference](../../docs/reference/observability.md#tracing-57-adr-0068).
- **Alerts** are evaluated by Prometheus from `infra/observability/prometheus/rules/`
  and visible in Grafana under **Alerting → Alert rules** (External — Prometheus).

Send OTLP traces to Tempo at `tempo:4317` (gRPC) / `tempo:4318` (HTTP) on the
internal network (also published on the host, loopback-bound by default).

## Alert rules

Three rules are pre-configured in `prometheus/rules/epicurus-alerts.yml`:

| Alert | Condition | Severity | For |
| --- | --- | --- | --- |
| `ServiceDown` | `up{job="epicurus-services"} == 0` | critical | 2 m |
| `OpenBaoSealed` | `probe_success{job="blackbox-openbao"} == 0` | critical | 1 m |
| `DiskSpaceHigh` | root filesystem > 85% (via node-exporter) | warning | 5 m |

Active alerts appear in Grafana **Alerting → Alert rules** and in Prometheus at
`http://localhost:9090/alerts`.

## Configuring notifications

Edit `infra/observability/alertmanager/alertmanager.yml` to add a receiver.
Uncomment the `webhook_configs` example and set your URL, or add an `email_configs`
block. Reload Alertmanager without restarting:

```bash
docker compose kill -s SIGHUP alertmanager
```

See [startup and recovery](../../docs/infrastructure/startup-and-recovery.md) for
recovery procedures when alerts fire.
