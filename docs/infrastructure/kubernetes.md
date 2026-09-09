# Kubernetes (the Helm chart)

The whole stack — the core runtime, the web shell, every module and the data
plane — as one Helm release. It lives in the repository at
[`infra/k8s/epicurus/`](../../infra/k8s/epicurus/Chart.yaml), beside
[`infra/compose/`](../../infra/compose/README.md), and changes in lockstep with the
services: a new module wires compose *and* the chart in the same PR
(`task new-module` does both), the chart's `appVersion` is the same image tag the
release pipeline publishes, and `chart-validate` runs in the same CI as the images
it deploys. Nothing about it is SaaS-shaped: the OSS chart is complete and
self-hostable on its own, and multi-tenant concerns layer on top of it as values
or a sub-chart, never the other way round (constraint #5).

It is packaging, not a second product. Everything the architecture already
required for this — stateless services with externalized state, swappable storage
and LLM backends, `/health` and `/metrics` on every service, images on GHCR, a
module↔core contract that is plain HTTP + NATS over an internal network — is
unchanged. The chart adds no code path the compose stack does not have, with one
exception: the core's container-runtime seam selects its Kubernetes
implementation here (`CONTAINER_RUNTIME=kubernetes`) instead of talking to a
Docker socket.

## Quick start

The standard path installs a tagged release straight from GHCR — the chart is a
release artifact, published as an OCI artifact beside the images by
`release.yml`, and the GHCR package `charts/epicurus` is public, so no
`helm registry login` is needed to pull it:

```bash
helm install epicurus oci://ghcr.io/baakhoff/charts/epicurus \
  --version <latest release, e.g. 0.2.0> \
  --namespace epicurus --create-namespace

# watch it come up (core-app waits on the OpenBao bootstrap job)
kubectl -n epicurus get pods -w

# back up the unseal key the bootstrap job just created — see "Data model"
kubectl -n epicurus get secret epicurus-openbao -o jsonpath='{.data.unseal-key}' | base64 -d

# reach the UI
kubectl -n epicurus port-forward svc/web 8084:8080
```

A release chart's `appVersion` is already the same tag as its images, so no
`--set image.tag=` is needed. See [Releases](../developer/releases.md) for the
list of tags.

### From a checkout (developing the chart)

Installing straight from `infra/k8s/epicurus/` is how you develop the chart
itself, not the recommended operator path — `Chart.yaml` carries a placeholder
`appVersion` (`latest`) that no image on GHCR is actually tagged, so pass
`--set image.tag=<a real tag>` or the pods will fail to pull:

```bash
helm install epicurus infra/k8s/epicurus --namespace epicurus --create-namespace \
  --set image.tag=<a release tag, e.g. 0.2.0>
```

Everything else — the quick-start commands above, the values reference below —
applies identically to a checkout install.

### Tracking a branch (opt-in)

Occasionally the owner wants a cluster running an exact, unreleased commit —
not a release, not a checkout. `chart-branch.yml` packages and pushes a chart
from any ref on a manual dispatch only; it never runs on a push, and it is not
a supported path for anyone but the owner's own testing clusters:

```bash
gh workflow run chart-branch.yml -f ref=testing
# a non-testing ref must also say which tag its images carry:
gh workflow run chart-branch.yml -f ref=some-branch -f image_tag=testing
```

The published version is `0.0.0-<branch>.<7-char sha>` (anything in the branch
name that is not a letter or a digit — the "/" in `feat/foo`, an "_" — is
flattened to "-", since a semver prerelease identifier allows only those three).
That sorts below every real release, so `helm upgrade` from a branch chart back
onto a tagged release is an ordinary upgrade — nothing about tracking a branch
leaves the install in a state a release can't supersede:

```bash
helm install epicurus oci://ghcr.io/baakhoff/charts/epicurus \
  --version 0.0.0-testing.abc1234 --namespace epicurus --create-namespace

# later, back onto a release
helm upgrade epicurus oci://ghcr.io/baakhoff/charts/epicurus --version 0.2.0
```

Before #907, `testing.yml` published `0.0.0-testing.<sha>` automatically on
every push to `testing`; that automatic job is gone; `chart-branch.yml`
replaces it as an explicit, on-demand action.

On a first install `core-app` (and `messaging`) sit in `Init:0/1` for the first
minute: they wait for the OpenBao bootstrap Job to write the app token. That Job
is a plain revision-named Job rather than a Helm hook precisely so this is a wait
and not a deadlock — Helm runs post-install hooks only *after* every normal
resource is ready, so a hook here would make `helm install --wait` (and Argo CD
and Flux, which wait by default) hang until it timed out. As a normal resource it
runs alongside the pods that are waiting for it.

> **One release per namespace.** Workloads and Services are named *bare* —
> `core-app`, `knowledge`, `postgres` — not `<release>-<name>`. That is
> load-bearing, not a style choice: the core locks a module's file-space folder by
> the hostname in `MODULE_URLS` (`CoreAppSettings.module_hostnames`, ADR-0063), and
> the whole codebase and every doc already speak these names. Install a second
> epicurus into its own namespace.

## What the chart renders

| Workload | Kind | Notes |
| --- | --- | --- |
| `core-app` | Deployment (`replicas: 1`, `Recreate`) | The runtime. Singleton in v1; RWO file PVC. |
| `web` | Deployment | The UI shell. The only thing an Ingress ever points at. |
| `echo`, `storage`, `knowledge`, `websearch`, `calendar`, `mail`, `tasks`, `notes`, `messaging` | Deployment each | One per entry in `modules`. |
| `postgres` | StatefulSet + PVC | `postgres:17`. |
| `nats` | StatefulSet + PVC | `nats:2.10`, JetStream, the compose `nats-server.conf` as a ConfigMap. |
| `qdrant` | StatefulSet + PVC | With the upgrade-guard init container (see below). |
| `openbao` | StatefulSet + PVC | Plus a bootstrap Job and an unseal Deployment. |
| `minio` | StatefulSet + PVC | On by default (as under Compose) — the `storage` module's object store. Plus a bucket-seeding Job. |
| `ollama` | StatefulSet + PVC | Optional (on by default), CPU by default, GPU hooks. |
| `searxng` | Deployment | Its `settings.yml` as a ConfigMap. |

Every workload carries `app.kubernetes.io/part-of: epicurus` and
`app.kubernetes.io/component: <name>`. That pair is a contract, not decoration:
it is how the core's Kubernetes container-runtime seam finds a module's Deployment
to scale to zero on a confirmed removal, and Ollama's StatefulSet to
rollout-restart after a KV-cache change. Renaming either label breaks those paths.

## The contract

The chart exposes no API of its own. What it *guarantees* to the code running
inside it:

**Service names and ports.** `core-app:8080`, `web:8080`, `<module>:8080`,
`postgres:5432`, `nats:4222` (monitoring `8222`), `qdrant:6333`/`6334`,
`openbao:8200`, `minio:9000`/`9001`, `ollama:11434`, `searxng:8080` — the same
names the compose stack uses, so every default in the code is already correct.

**Environment.** Rendered from Service names, never hardcoded: `MODULE_URLS`,
`PLATFORM_URL`, `ATTACHMENT_SINK_URL`, `DATABASE_URL`, `NATS_URL`, `QDRANT_URL`,
`OPENBAO_URL`, `OLLAMA_URL`, `SEARXNG_URL`, `MINIO_*`. Every key is a setting that
already exists — see [`config`](../reference/config.md). Credentials come from a
Secret; a password is never written into a ConfigMap, and `DATABASE_URL` embeds
`$(POSTGRES_PASSWORD)`, which the kubelet expands from the env var declared above
it in the same container, so the DSN itself carries no secret.

**The container-runtime seam.** The implementation lives in the core (#891); the
chart's job is to select it and to grant it exactly what it needs.
`core-app` gets `CONTAINER_RUNTIME=kubernetes` and
`KUBERNETES_NAMESPACE` from the downward API (never guessed), plus a Role granting
`get`/`list`/`patch` on `deployments`, `deployments/scale` and `statefulsets` in
the release namespace — and nothing else. It can read the workloads, patch a
scale, and patch a pod template; it can create and delete nothing, and it can
never reach another namespace.

**Ingress: `web` only.** The module↔core contract is local-only (constraint #7).
No template in this chart can expose a module or the core. Reach one with
`kubectl port-forward` when you need to.

## Configuration

Every value, with its default. Anything not listed is not a key.

### Images and stack-wide

| Key | Default | Meaning |
| --- | --- | --- |
| `image.registry` | `ghcr.io/baakhoff` | Registry + org for the first-party images. |
| `image.repositoryPrefix` | `epicurus-` | So `core-app` → `ghcr.io/baakhoff/epicurus-core-app`. |
| `image.tag` | `""` | Blank = `.Chart.AppVersion` (set by the release pipeline). |
| `image.pullPolicy` | `IfNotPresent` | Applied to every container. |
| `image.pullSecrets` | `[]` | Names of imagePullSecrets in the namespace. |
| `defaultTenantId` | `local` | `DEFAULT_TENANT_ID`. Tenant is first-class even at one tenant. |
| `logLevel` | `info` | `LOG_LEVEL`. |
| `appEnv` | `production` | `APP_ENV`. Anything but `local` renders JSON logs. |
| `tracing.enabled` | `false` | `OTEL_TRACES_ENABLED`. |
| `tracing.endpoint` | `http://tempo:4318` | `OTEL_EXPORTER_OTLP_ENDPOINT`. The chart ships no collector. |
| `storageClass` | `""` | Applied to every PVC the chart creates; blank = cluster default. |
| `commonLabels` | `{}` | Merged onto every workload's labels. |
| `commonAnnotations` | `{}` | Merged onto every workload's annotations. |

### Secrets

| Key | Default | Meaning |
| --- | --- | --- |
| `secrets.existingSecret` | `""` | Use a Secret you manage; the chart then renders none. |
| `secrets.postgresPassword` | `""` | Blank = keep the deployed value, else generate. |
| `secrets.natsCorePassword` | `""` | as above |
| `secrets.natsModulePassword` | `""` | as above |
| `secrets.natsSysPassword` | `""` | as above |
| `secrets.oauthStateSecret` | `""` | as above |
| `secrets.minioRootUser` | `""` | Blank = `epicurus`. |
| `secrets.minioRootPassword` | `""` | Blank = keep the deployed value, else generate. |

### `core` — the runtime

| Key | Default | Meaning |
| --- | --- | --- |
| `core.image.repository` / `.tag` | `""` / `""` | Override just this image. |
| `core.resources` | `requests: 100m / 512Mi` | Standard resources block (add `limits` if you want them). |
| `core.filesBackend` | `local` | `FILES_BACKEND`. `local` = the PVC below; `s3` = a per-tenant bucket. |
| `core.persistence.enabled` | `true` | The file-space PVC. `false` = an emptyDir (data is lost on restart). |
| `core.persistence.existingClaim` | `""` | Use a claim you already have. |
| `core.persistence.accessMode` | `ReadWriteOnce` | The singleton makes RWO sufficient. |
| `core.persistence.size` | `20Gi` | |
| `core.persistence.storageClass` | `""` | Falls back to `storageClass`. |
| `core.podSecurityContext` | `{}` | See "The core's uid dance" below. |
| `core.securityContext` | `runAsUser: 0`, no privilege escalation, drop ALL, add `CHOWN`/`SETUID`/`SETGID`/`FOWNER`/`DAC_OVERRIDE` | as above |
| `core.containerRuntime.kind` | `kubernetes` | `CONTAINER_RUNTIME`. `none` disables container control. |
| `core.containerRuntime.rbac.create` | `true` | Render the ServiceAccount + Role + RoleBinding. |
| `core.containerRuntime.rbac.serviceAccountName` | `""` | Use an account you manage instead. |
| *(no key)* | — | `EPICURUS_VERSION` is set for you, to `image.tag` or the chart's `appVersion` — the tag this pod actually runs. It is what `/platform/v1/info` reports as `release_track`, and what the Settings → Platform card shows as the track. |
| `core.llm.defaultModel` | `llama3.2` | `LLM_DEFAULT_MODEL`. |
| `core.llm.keepAlive` | `5m` | `LLM_KEEP_ALIVE`. |
| `core.llm.fallbacks` | `""` | `LLM_FALLBACKS`. |
| `core.llm.numRetries` | `2` | `LLM_NUM_RETRIES`. |
| `core.llm.timeout` | `1800` | `LLM_TIMEOUT` (inter-chunk read timeout, seconds). |
| `core.llm.temperature` / `.topP` / `.numCtx` | `""` | Blank = the provider default. |
| `core.llm.bootstrapModels` | `auto` | `LLM_BOOTSTRAP_MODELS`; `""` disables the first-boot pull. |
| `core.memoryEmbedModel` | `nomic-embed-text` | `MEMORY_EMBED_MODEL`. |
| `core.oauth.redirectBaseUrl` | `""` | `OAUTH_REDIRECT_BASE_URL`; blank derives it from `ingress.host`. |
| `core.podAnnotations` | `{}` | Merged with the metrics annotations. |
| `core.nodeSelector` / `.tolerations` / `.affinity` | `{}` / `[]` / `{}` | Scheduling. |
| `core.extraEnv` | `{}` | Any other setting, as a `NAME: value` map. Applied last. |

`core.extraEnv` is how every setting the chart does not name explicitly is set —
maintenance schedules, memory tuning, portability ceilings, external file mounts.
The full list is in [`config`](../reference/config.md).

### `web`

| Key | Default | Meaning |
| --- | --- | --- |
| `web.enabled` | `true` | |
| `web.replicas` | `1` | Stateless nginx; scale freely. |
| `web.image.repository` / `.tag` | `""` / `""` | |
| `web.resources` | `requests: 20m / 64Mi` | |
| `web.podAnnotations` / `.nodeSelector` / `.tolerations` / `.affinity` | empty | |
| `web.extraEnv` | `{}` | |

The only env the chart sets is `CORE_APP_URL=http://core-app:8080`. The shell's
nginx proxies `/platform/` through a variable, so it resolves that name at request
time — and the image derives the resolver it uses from `/etc/resolv.conf` at
start-up, which is what makes it work in a pod. That is a sibling change to this
chart (#891); a web image built before it hardcodes Docker's embedded DNS
(`127.0.0.11`), and against such an image every `/platform/` request 502s while
both probes stay green, because `/healthz` is a static handler that never touches
the resolver.

### `modules` and `moduleDefaults`

`modules.<name>.enabled` (`true` for all nine) switches a module on. Each entry
also takes `replicas`, `image.repository`/`.tag`, `resources`, `podAnnotations`,
`nodeSelector`, `tolerations`, `affinity` and `env`, each falling back to
`moduleDefaults` (`replicas: 1`, `requests: 50m / 256Mi`, the rest empty).

`modules.<name>.wants` decides which shared endpoints and credentials the pod is
handed — the chart's version of what each module's compose fragment sets, so a
module never receives a credential it does not use:

| `wants` flag | What the pod gets |
| --- | --- |
| `database` | `POSTGRES_PASSWORD` (from the Secret) + `DATABASE_URL` |
| `qdrant` | `QDRANT_URL` |
| `platform` | `PLATFORM_URL` |
| `searxng` | `SEARXNG_URL` |
| `minio` | `MINIO_URL`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` |
| `openbao` | `OPENBAO_URL`, `OPENBAO_TOKEN_FILE` + the token mount and its init container |

Shipped defaults: `storage` (database, platform, minio) · `knowledge` (database,
qdrant, platform; `VAULT_PATH=/data/knowledge`) · `notes` (database, qdrant,
platform; `NOTES_ROOT=/data/notes`) · `websearch` (platform, searxng) ·
`calendar`/`mail`/`tasks` (database, platform) · `messaging` (openbao;
`MESSAGING_PROVIDER=loopback`) · `echo` (nothing).

Every module also gets the common env: `APP_ENV`, `LOG_LEVEL`,
`DEFAULT_TENANT_ID`, `NATS_URL`, `NATS_USER=module`, `NATS_PASSWORD`,
`OTEL_TRACES_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`.

### `ingress` (web only)

| Key | Default | Meaning |
| --- | --- | --- |
| `ingress.enabled` | `false` | |
| `ingress.className` | `nginx` | |
| `ingress.host` | `epicurus.example.com` | Also the default `OAUTH_REDIRECT_BASE_URL`. |
| `ingress.path` / `.pathType` | `/` / `Prefix` | |
| `ingress.annotations` | the four below | Replace wholesale for a non-nginx controller. |
| `ingress.tls.enabled` | `false` | |
| `ingress.tls.secretName` | `""` | Blank with TLS on = the controller's default certificate. |

The default annotations carry a lesson, not a preference:

```yaml
nginx.ingress.kubernetes.io/proxy-body-size: "0"
nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
```

ingress-nginx defaults to a small request-body cap and buffered request bodies.
A tenant-archive import is a multi-hundred-megabyte upload and chat is a streamed
response, so both defaults break the product before the core sees a byte — the
same failure the web shell's own nginx had (#887, and the reason the import route
got its own uncapped location). With `proxy-body-size: "0"` the core's own
`PORTABILITY_MAX_ARCHIVE_MB` is the only ceiling, which is where a ceiling
belongs. If you swap the controller, port these four settings to its dialect.

### `networkPolicy`

| Key | Default | Meaning |
| --- | --- | --- |
| `networkPolicy.enabled` | `false` | Needs a CNI that enforces policy (Calico, Cilium). |
| `networkPolicy.webIngressFrom` | `[]` | Extra selectors allowed to reach `web`; empty = any source. |

Four ingress-only policies: default-deny for everything labelled
`part-of=epicurus`, then `web` from anywhere (an ingress controller lives in its
own namespace), `core-app` from the release's own pods, each module from
`core-app` alone, and the data plane from the release's own pods. Egress is
deliberately unrestricted — websearch, Ollama model pulls and hosted LLM providers
all need it, and a default-deny egress is a footgun that would also take DNS with
it.

### `metrics`

| Key | Default | Meaning |
| --- | --- | --- |
| `metrics.podAnnotations` | `true` | `prometheus.io/scrape|port|path` on the pods that serve `/metrics`. |
| `metrics.podMonitor.enabled` | `false` | A PodMonitor for kube-prometheus-stack (needs its CRDs). |
| `metrics.podMonitor.namespace` | `""` | Blank = the release namespace. |
| `metrics.podMonitor.interval` | `30s` | |
| `metrics.podMonitor.labels` | `{}` | Labels your Prometheus' `podMonitorSelector` matches. |

Only `core-app` and the modules serve `/metrics`; they carry
`epicurus.io/metrics: "true"` and the PodMonitor selects on it. The web shell is
nginx and the data plane speaks its own protocols. The chart re-ships no
Prometheus, no rules and no dashboards — that is phase 2 (#896).

### Data plane

Each piece is either in-chart (`enabled: true`) or an external endpoint
(`enabled: false` + `external.*`), so a homelab installs one release and a real
cluster points at managed services.

| Key | Default |
| --- | --- |
| `postgres.enabled` | `true` |
| `postgres.image.repository` / `.tag` | `postgres` / `17` |
| `postgres.auth.username` / `.database` | `epicurus` / `epicurus` |
| `postgres.persistence.enabled` / `.existingClaim` / `.size` / `.storageClass` | `true` / `""` / `20Gi` / `""` |
| `postgres.resources` | `requests: 100m / 256Mi` |
| `postgres.podSecurityContext` | `fsGroup: 999` |
| `postgres.external.host` / `.port` | `""` / `5432` |
| `nats.enabled` | `true` |
| `nats.image.repository` / `.tag` | `nats` / `2.10` |
| `nats.config` | `""` (blank = the compose `nats-server.conf`) |
| `nats.persistence.*` | as postgres, `10Gi` |
| `nats.resources` | `requests: 50m / 128Mi` |
| `nats.external.url` | `""` |
| `qdrant.enabled` | `true` |
| `qdrant.image.repository` / `.tag` | `qdrant/qdrant` / `v1.18.2` |
| `qdrant.initImage.repository` / `.tag` | `alpine` / `3.21` |
| `qdrant.persistence.*` | as postgres, `20Gi` |
| `qdrant.resources` | `requests: 100m / 512Mi` |
| `qdrant.external.url` | `""` |
| `openbao.enabled` | `true` |
| `openbao.image.repository` / `.tag` | `openbao/openbao` / `2.2.0` |
| `openbao.persistence.*` | as postgres, `2Gi` |
| `openbao.resources` | `requests: 50m / 128Mi` |
| `openbao.bootstrap.enabled` | `true` |
| `openbao.bootstrap.image.repository` / `.tag` | `curlimages/curl` / `8.11.1` |
| `openbao.bootstrap.secretName` | `epicurus-openbao` |
| `openbao.bootstrap.storeNatsPasswords` | `true` |
| `openbao.bootstrap.activeDeadlineSeconds` | `1800` |
| `openbao.bootstrap.ttlSecondsAfterFinished` | `86400` |
| `openbao.unseal.enabled` / `.intervalSeconds` | `true` / `30` |
| `openbao.external.url` / `.tokenSecret` / `.tokenSecretKey` | `""` / `""` / `app-token` |
| `minio.enabled` | **`true`** |
| `minio.image.repository` / `.tag` | `minio/minio` / `RELEASE.2025-04-22T22-12-26Z` |
| `minio.initImage.repository` / `.tag` | `minio/mc` / `RELEASE.2025-04-16T18-13-26Z` |
| `minio.defaultBucket` | `epicurus` |
| `minio.initJob.ttlSecondsAfterFinished` | `86400` |
| `minio.persistence.*` | as postgres, `50Gi` |
| `minio.resources` | `requests: 100m / 256Mi` |
| `minio.external.url` | `""` |
| `ollama.enabled` | `true` |
| `ollama.image.repository` / `.tag` | `ollama/ollama` / `0.30.7` |
| `ollama.persistence.*` | as postgres, `100Gi` |
| `ollama.resources` | `requests: 500m / 4Gi` (add `limits` — memory especially) |
| `ollama.env` | `OLLAMA_KEEP_ALIVE: 5m`, `OLLAMA_FLASH_ATTENTION: "0"`, `OLLAMA_KV_CACHE_TYPE: f16` |
| `ollama.gpu.enabled` / `.count` / `.resourceName` / `.runtimeClassName` | `false` / `1` / `nvidia.com/gpu` / `""` |
| `ollama.external.url` | `""` |
| `searxng.enabled` | `true` |
| `searxng.image.repository` / `.tag` | `searxng/searxng` / `2026.6.10-de03f4eb1` |
| `searxng.settings` | `""` (blank = the compose `settings.yml`) |
| `searxng.replicas` | `1` |
| `searxng.resources` | `requests: 50m / 256Mi` |
| `searxng.external.url` | `""` |

Every `resources` key in this chart is a standard Kubernetes resources block: the
defaults set `requests` only, and `limits` are yours to add. Each data-plane block
also takes `nodeSelector`, `tolerations` and `affinity`.
Every `external.url` is required once its `enabled` is `false` — Helm fails the
render with a named message rather than deploying something that cannot connect.
Postgres is the exception in shape: an external server is addressed by
`external.host`/`.port` and the credentials still come from the Secret, so no DSN
with a password in it ever sits in a values file.

**MinIO is on by default**, matching the Compose stack. It backs two different
things: the `storage` module's object store (chat uploads, agent-written objects,
the byte half of a tenant export) and, optionally, `core.filesBackend: s3`. It is
on because `storage` is on: with no object store the module still starts and its
file-index half still works, but every object operation fails at call time — a
half-working module is a worse default than a PVC. If you would rather not run
it, either point `minio.external.url` at your own S3 (credentials still come from
the Secret) or set `minio.enabled: false` *and* disable the `storage` module;
`NOTES.txt` warns after an install that is in the half-working state.

## Data model

**PersistentVolumeClaims.** ReadWriteOnce throughout — the data-plane ones
unconditionally, the core's file claim by default
(`core.persistence.accessMode`, which you would only raise to ReadWriteMany for
an experiment the singleton does not need). The StatefulSets use
`volumeClaimTemplates`, so Helm never deletes them.

| Claim | Owner | Holds |
| --- | --- | --- |
| `core-app-files` | core-app | The tenant file space at `/data/<tenant>` — knowledge vaults, notes' `.md` mirror, everything the Files view browses. |
| `data-postgres-0` | postgres | Every service's tables (`agent_messages`, `storage_files`, `knowledge_notes`, …), all tenant-scoped. |
| `data-nats-0` | nats | The JetStream store — the event spine's durable half. |
| `data-qdrant-0` | qdrant | Vectors. **Derived data**: knowledge re-embeds from the file space, memory re-embeds from Postgres. |
| `data-openbao-0` | openbao | The secret store (file backend), including every per-tenant provider credential. |
| `data-minio-0` | minio | The object store, when enabled. |
| `models-ollama-0` | ollama | Downloaded model weights. Large; the one PVC that regularly needs raising. |

**Secrets.** Two, and they are very different animals:

- **`epicurus-secrets`** — Helm-managed. Exactly seven keys:
  `POSTGRES_PASSWORD`, `NATS_CORE_PASSWORD`, `NATS_MODULE_PASSWORD`,
  `NATS_SYS_PASSWORD`, `OAUTH_STATE_SECRET`, `MINIO_ROOT_USER`,
  `MINIO_ROOT_PASSWORD`. A Secret you supply through `secrets.existingSecret` must
  carry **all seven** — a missing one is not a render error, it is a
  `CreateContainerConfigError` on the pods that reference it. Keep
  `POSTGRES_PASSWORD` URL-safe: it is interpolated into a DSN. Generated on first
  install and **kept across
  `helm upgrade`**: the template reads the live Secret back with `lookup` before
  generating anything, so an upgrade never rotates a password out from under a
  running Postgres. `helm uninstall` **deletes** it while the PVCs survive, and a
  reinstall would then generate a `POSTGRES_PASSWORD` that does not match the
  existing database — so for anything you care about, manage it yourself with
  `secrets.existingSecret`.
- **`epicurus-openbao`** — written by the bootstrap Job through the Kubernetes
  API, so Helm neither owns nor deletes it. `unseal-key`, `root-token`,
  `app-token`. **Back up the unseal key outside the cluster.** Without it,
  everything on the OpenBao PVC is unreachable — every provider credential, every
  connected account. Nothing else protects it.

`helm template` shows freshly generated passwords, because `lookup` has no
cluster to read; those are not the deployed values. No credential is ever
rendered into a ConfigMap.

## Dependencies

- **A Kubernetes cluster, 1.25 or newer**, with a default StorageClass (or set
  `storageClass`). k3s with its local-path provisioner is enough.
- **An ingress controller**, only if `ingress.enabled`. The default annotations
  assume ingress-nginx.
- **A CNI that enforces NetworkPolicy**, only if `networkPolicy.enabled`.
  k3s' default flannel does **not**.
- **kube-prometheus-stack**, only if `metrics.podMonitor.enabled` (for its CRDs).
- **Outbound network** for the Ollama model pull on first boot, the model catalog,
  websearch, and any hosted LLM provider.
- **Images on GHCR** (`ghcr.io/baakhoff/epicurus-*`), plus the third-party images
  named in the table above.

## Run and extend

### Adding a module

`task new-module -- "My Module"` writes the chart entry along with everything
else (compose include, the core's `module_urls`, the port registry, the smoke CI
override). Then add the `wants` the module actually uses.
`tests/test_chart_services.py` fails if the chart's `modules` map and the compose
`include:` list ever disagree, so a hand-rolled module cannot skip this — the same
"wire it in completely" rule the runtime smoke gate enforces for compose. See
[Building a module](../developer/building-a-module.md).

### Validating a change

```bash
helm lint infra/k8s/epicurus
helm template epicurus infra/k8s/epicurus \
  | kubeconform -strict -summary -kubernetes-version 1.25.0
helm template epicurus infra/k8s/epicurus \
  --set ingress.enabled=true,minio.enabled=true,networkPolicy.enabled=true,metrics.podMonitor.enabled=true \
  | kubeconform -strict -summary -kubernetes-version 1.25.0 -schema-location default \
      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

That is exactly what CI's `chart-validate` job runs, against Kubernetes 1.25
schemas. The second render adds the CRDs-catalog schema location so the optional
PodMonitor is really validated rather than skipped — a skipped resource validates
nothing and still reports success.

The OpenBao bootstrap script is exercised for real in
`tests/test_chart_services.py`, against a stub of the OpenBao and Kubernetes APIs:
fresh init, a re-run that changes nothing, a restarted (re-sealed) vault, a
revoked app token, and the refusal when the unseal key is gone. It is also
shellchecked by the `shell-lint` gate like every other script in the repo. No
cluster is booted anywhere in CI; a kind-based `k8s-smoke` mirroring
`infra/ci/smoke.sh` is filed as a follow-up.

### Upgrading

```bash
helm upgrade epicurus infra/k8s/epicurus --namespace epicurus -f my-values.yaml
```

Keep your values in a file and pass it every time. Avoid `--reuse-values`: it
overlays the *previous* release's computed values onto the new chart, so keys a
chart upgrade adds silently keep their old shape. `--reset-then-reuse-values`
(Helm 3.14+) is the safe version of the same idea.

What happens: passwords are kept (`lookup`), the OpenBao bootstrap Job is recreated
under the new revision's name and is a no-op when nothing needs doing, `core-app` uses the
`Recreate` strategy so two replicas never hold the RWO file PVC at once, and a
`qdrant` image-tag change wipes the vector store on purpose — its on-disk segment
format is version-bound, a mismatched one panics the server on boot (#229), and
the vectors are derived data that a background re-index repopulates. See
[Qdrant](qdrant.md).

To pin a specific image tag independently of the chart:
`--set image.tag=0.3.0`.

### The core's uid dance

The core image's entrypoint starts as **root**, creates and `chown`s *only*
`<FILES_ROOT>/<tenant>` to uid 10001, then setuids to 10001 and execs the app
(#421, ADR-0069). The chown is surgical — never recursive — so an operator's
existing tree is left untouched. The chart's default `core.securityContext`
reflects exactly that: `runAsUser: 0`, no privilege escalation, all capabilities
dropped except `CHOWN`, `SETUID`, `SETGID`, `FOWNER`, `DAC_OVERRIDE`.

The entrypoint *does* tolerate a non-root start (it skips provisioning and just
execs). So on a namespace under the `restricted` Pod Security Standard you can
instead set:

```yaml
core:
  podSecurityContext:
    fsGroup: 10001
    runAsUser: 10001
    runAsNonRoot: true
  securityContext:
    runAsUser: 10001
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]
```

with the caveat that the tenant root is then created by the app rather than the
entrypoint, which is the untested path. The root-start default is the one the
image was built for, and it is what the compose stack does.

OpenBao's StatefulSet makes the same trade for the same reason: its entrypoint
drops to uid 100, which cannot write a freshly provisioned root-owned volume, so
the container starts as root and `chown`s the data directory *by name* (never a
guessed uid) before exec'ing the image entrypoint — the compose fragment's exact
handoff.

### OpenBao: bootstrap, unseal, recover

Three pieces, mirroring the compose stack:

1. **The bootstrap Job** — `openbao-bootstrap-<revision>`, a normal resource, not
   a Helm hook (see the quick start for why). It waits for the API; if the vault is
   uninitialised it first **proves it can write** `epicurus-openbao` with a
   throwaway key, because a vault created by a job that then cannot store the key
   is unrecoverable; initialises 1-of-1 Shamir, stores the unseal key and root
   token *before doing anything else*, unseals,
   enables KV v2 at `secret/`, writes the `epicurus-core` policy, and mints a
   **periodic** app token (768h period, renewed daily by core-app). Periodic is
   load-bearing: a plain service token silently falls back to the default lease and
   every secret read starts failing about a month after bootstrap (#728). Re-runs
   are no-ops, including the token — it is re-minted only if the stored one no
   longer authenticates, and the superseded one is revoked so orphaned periodic
   tokens cannot pile up across upgrades.
2. **The unseal Deployment** submits the key again whenever the vault reports
   sealed, so a restarted OpenBao pod comes back on its own. It reads the key from
   the same Secret, mounted `optional`, and waits for the file on a first install.
3. **App pods** mount `app-token` at `OPENBAO_TOKEN_FILE`
   (`/var/run/secrets/epicurus/openbao-token`) and an init container blocks until
   it appears — which is why `core-app` and `messaging` sit in `Init:0/1` for the
   first minute of a fresh install. `resolve_openbao_token` re-reads the file on
   every re-authentication, so rotating the token needs no restart.

The Job's ServiceAccount may `create` a Secret in the namespace and `get`/`patch`
exactly `epicurus-openbao` (`create` cannot be narrowed by name, because the
object does not exist yet). It has no other permission.

**Recovery.** If `epicurus-openbao` is lost but the PVC survives, the Job refuses
to guess and fails with a named error — restore the Secret from your backup, or
delete the OpenBao PVC to start over, which destroys every stored secret and means
reconnecting every provider account. With an **external** OpenBao
(`openbao.enabled: false`), the chart cannot mint a token: name a Secret you
manage in `openbao.external.tokenSecret` and app pods mount it identically.

See [Secrets (OpenBao)](secrets.md) for what lives in the vault.

## Known limitations

- **`core-app` is a singleton.** The local file backend's inotify watcher, the
  in-process live-run registry and the portability staging directory are
  single-writer. Lifting it (S3 file backend by default, a shared live-run
  registry, then PodDisruptionBudgets and an HPA) is a follow-up, not a values
  toggle.
- **The Ollama KV-cache "Apply" does not take effect.** On compose the core writes
  `/etc/epicurus/ollama.env` into a volume Ollama re-reads on restart. In a cluster
  that would need a ReadWriteMany volume, which is not a safe default, so the core
  writes it to an emptyDir only it can see. The rollout-restart still happens; the
  new setting does not reach Ollama. Set `ollama.env.OLLAMA_KV_CACHE_TYPE` (and
  `OLLAMA_FLASH_ATTENTION`) in values instead — a `helm upgrade` restarts the pod
  with them.
- **No observability stack.** Pods carry scrape annotations and there is an
  optional PodMonitor; rules, dashboards and alert routing on Kubernetes are
  phase 2.
- **No backup/restore.** `infra/backups/` is compose-shaped (it loops over Docker
  volumes). PVC-aware backup is a filed follow-up; until then, back up
  `epicurus-openbao` and snapshot the PVCs with your cluster's own tooling.
- **No `/ready` distinct from `/health`.** Both probes hit the same endpoint, so a
  service that is up but not yet warm still reports ready.
- **No kind-based smoke gate yet.** `chart-validate` proves the chart renders and
  schema-validates; it does not prove the stack boots. `runtime-smoke` still does
  that for compose, and the same assertions on a kind cluster are filed.

## What is *not* in the chart, on purpose

- **Valkey.** Nothing in the codebase uses it (only denylists mention it).
  Removing it from compose is a separate decision.
- **Traefik / the edge gateway.** A cluster already has an ingress controller;
  the compose stack's gateway has no job here.
- **The Docker-socket proxy.** There is no Docker daemon in a cluster — the
  container-runtime seam replaces it.
- **Grafana / Loki / Prometheus / Tempo.** See above.
- **SaaS concerns** — billing, metering, quotas, signup. They live in the private
  overlay and layer onto this chart, never into it (constraint #5).
