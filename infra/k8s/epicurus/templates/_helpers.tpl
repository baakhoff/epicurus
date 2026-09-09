{{/*
Shared helpers.

Two conventions run through this chart and every template depends on them:

  * **Bare names.** Workloads and Services are named `core-app`, `knowledge`,
    `postgres` — never `<release>-<name>`. The core locks a module's file-space
    folder by the hostname in MODULE_URLS (ADR-0063), so the Service name IS part
    of the contract. One release per namespace.
  * **`app.kubernetes.io/part-of: epicurus` + `app.kubernetes.io/component: <name>`
    on every workload.** That pair is how the core's Kubernetes container-runtime
    seam (#891) finds a module's Deployment to scale to zero, and Ollama's
    StatefulSet to rollout-restart. Do not rename either label.
  * **`enableServiceLinks: false` on every pod spec.** Kubernetes otherwise injects a
    `{SVCNAME}_SERVICE_HOST` and `{SVCNAME}_PORT` env var into every pod for every
    Service in the namespace — Docker-links compatibility nothing here uses, because
    every endpoint this chart wires comes from an env var it sets explicitly. It is
    not merely noise: those generated names land in the same namespace as the stack's
    own variables, and one of them is fatal. `SEARXNG_PORT` arrives as
    `tcp://10.96.x.x:8080`; SearXNG's entrypoint does
    `export GRANIAN_PORT="${SEARXNG_PORT:-$GRANIAN_PORT}"`; the server then dies on a
    URL where it wanted a port number. The chart's very first boot on a cluster (#894)
    crash-looped on exactly that, and it is invisible under Compose, which injects
    nothing of the kind. A new pod spec gets this line too.

Helpers that need more than the root context take a dict, by convention
`(dict "ctx" $ "component" "web")`.
*/}}

{{- define "epicurus.selectorLabels" -}}
app.kubernetes.io/name: epicurus
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "epicurus.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .ctx.Chart.Name .ctx.Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "epicurus.selectorLabels" . }}
app.kubernetes.io/part-of: epicurus
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
{{- if .ctx.Chart.AppVersion }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
{{- end }}
{{- with .ctx.Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Fully-qualified image ref for a first-party service image. */}}
{{- define "epicurus.image" -}}
{{- $ctx := .ctx -}}
{{- $override := default (dict) .override -}}
{{- $repo := default (printf "%s/%s%s" $ctx.Values.image.registry $ctx.Values.image.repositoryPrefix .name) $override.repository -}}
{{- $tag := default (default $ctx.Chart.AppVersion $ctx.Values.image.tag) $override.tag -}}
{{- printf "%s:%s" $repo ($tag | toString) -}}
{{- end -}}

{{/* Fully-qualified image ref for a third-party image (repository + tag in values). */}}
{{- define "epicurus.thirdPartyImage" -}}
{{- printf "%s:%s" .repository (.tag | toString) -}}
{{- end -}}

{{/* Emits nothing at all when no pull secrets are configured — call it wrapped in
`with` so an empty result never leaves a stray line behind. */}}
{{- define "epicurus.imagePullSecrets" -}}
{{- if .Values.image.pullSecrets -}}
imagePullSecrets:
{{- range .Values.image.pullSecrets }}
  - name: {{ . }}
{{- end }}
{{- end -}}
{{- end -}}

{{/* The Secret every service reads shared credentials from. */}}
{{- define "epicurus.secretName" -}}
{{- default "epicurus-secrets" .Values.secrets.existingSecret -}}
{{- end -}}

{{/* storageClassName for a PVC: the component's own, else the stack default. */}}
{{- define "epicurus.storageClass" -}}
{{- $class := default .ctx.Values.storageClass .class -}}
{{- if $class -}}
storageClassName: {{ $class | quote }}
{{- end }}
{{- end -}}

{{/* ── Endpoints. Each is either the in-chart Service or an external one. ── */}}

{{- define "epicurus.natsUrl" -}}
{{- if .Values.nats.enabled -}}
nats://nats:4222
{{- else -}}
{{- required "nats.external.url is required when nats.enabled is false" .Values.nats.external.url -}}
{{- end -}}
{{- end -}}

{{- define "epicurus.qdrantUrl" -}}
{{- if .Values.qdrant.enabled -}}
http://qdrant:6333
{{- else -}}
{{- required "qdrant.external.url is required when qdrant.enabled is false" .Values.qdrant.external.url -}}
{{- end -}}
{{- end -}}

{{- define "epicurus.openbaoUrl" -}}
{{- if .Values.openbao.enabled -}}
http://openbao:8200
{{- else -}}
{{- required "openbao.external.url is required when openbao.enabled is false" .Values.openbao.external.url -}}
{{- end -}}
{{- end -}}

{{- define "epicurus.ollamaUrl" -}}
{{- if .Values.ollama.enabled -}}
http://ollama:11434
{{- else -}}
{{- required "ollama.external.url is required when ollama.enabled is false" .Values.ollama.external.url -}}
{{- end -}}
{{- end -}}

{{- define "epicurus.searxngUrl" -}}
{{- if .Values.searxng.enabled -}}
http://searxng:8080
{{- else -}}
{{- required "searxng.external.url is required when searxng.enabled is false" .Values.searxng.external.url -}}
{{- end -}}
{{- end -}}

{{- define "epicurus.minioUrl" -}}
{{- if .Values.minio.enabled -}}
http://minio:9000
{{- else -}}
{{- default "http://minio:9000" .Values.minio.external.url -}}
{{- end -}}
{{- end -}}

{{- define "epicurus.platformUrl" -}}
http://core-app:8080
{{- end -}}

{{/*
The core's URL *as nginx must be given it* — fully qualified, unlike every other
endpoint in this chart.

The web shell resolves the core at request time (so nginx keeps serving the UI
while the core restarts), which means a `resolver` directive and a runtime lookup
rather than a start-up one. nginx's resolver does NOT apply /etc/resolv.conf's
`search` list: it asks for exactly the name it was given. In a pod, `core-app.`
is not in the cluster DNS zone, so the query SERVFAILs and every /platform/
request 502s while both probes stay green — /healthz is a static handler that
never touches the resolver. The chart's first boot on a cluster (#894) hit exactly
that; under Compose, Docker's embedded DNS answers bare service names, so nothing
before could see it.

Every other consumer resolves through the OS resolver, which does apply `search`,
and keeps the bare Service name the code and docs speak (ADR-0063).
*/}}
{{- define "epicurus.webCoreAppUrl" -}}
{{- if .Values.web.coreAppUrl -}}
{{- .Values.web.coreAppUrl -}}
{{- else -}}
{{- printf "http://core-app.%s.svc.%s:8080" .Release.Namespace .Values.clusterDomain -}}
{{- end -}}
{{- end -}}

{{/*
The async Postgres DSN. The password is NEVER templated in: it is referenced as
`$(POSTGRES_PASSWORD)`, which the kubelet expands from the env var above it in the
same container — so the credential lives only in the Secret, never in a rendered
ConfigMap or a `helm template` diff. Keep the password URL-safe.
*/}}
{{- define "epicurus.databaseUrl" -}}
{{- $host := "postgres" -}}
{{- $port := 5432 -}}
{{- if not .Values.postgres.enabled -}}
{{- $host = required "postgres.external.host is required when postgres.enabled is false" .Values.postgres.external.host -}}
{{- $port = .Values.postgres.external.port -}}
{{- end -}}
{{- printf "postgresql+asyncpg://%s:$(POSTGRES_PASSWORD)@%s:%v/%s" .Values.postgres.auth.username $host $port .Values.postgres.auth.database -}}
{{- end -}}

{{/* Enabled module names, sorted — the single source of both MODULE_URLS and the workloads. */}}
{{- define "epicurus.enabledModules" -}}
{{- $names := list -}}
{{- range $name, $cfg := .Values.modules -}}
{{- if $cfg.enabled -}}
{{- $names = append $names $name -}}
{{- end -}}
{{- end -}}
{{- join "," (sortAlpha $names) -}}
{{- end -}}

{{- define "epicurus.moduleUrls" -}}
{{- $urls := list -}}
{{- range $name := (splitList "," (include "epicurus.enabledModules" .)) -}}
{{- if $name -}}
{{- $urls = append $urls (printf "http://%s:8080" $name) -}}
{{- end -}}
{{- end -}}
{{- join "," $urls -}}
{{- end -}}

{{/* The module that durably keeps chat uploads (ADR-0025); empty disables the sink. */}}
{{- define "epicurus.attachmentSinkUrl" -}}
{{- if and .Values.modules.storage .Values.modules.storage.enabled -}}
http://storage:8080
{{- end -}}
{{- end -}}

{{- define "epicurus.oauthRedirectBaseUrl" -}}
{{- if .Values.core.oauth.redirectBaseUrl -}}
{{- .Values.core.oauth.redirectBaseUrl -}}
{{- else if .Values.ingress.enabled -}}
{{- if .Values.ingress.tls.enabled -}}
https://{{ .Values.ingress.host }}
{{- else -}}
http://{{ .Values.ingress.host }}
{{- end -}}
{{- else -}}
http://localhost:8084
{{- end -}}
{{- end -}}

{{/* Scrape annotations for the pods that actually serve /metrics. */}}
{{- define "epicurus.metricsAnnotations" -}}
{{- if .Values.metrics.podAnnotations }}
prometheus.io/scrape: "true"
prometheus.io/port: "8080"
prometheus.io/path: "/metrics"
{{- end }}
{{- end -}}

{{/*
Env shared by every first-party service: identity, the bus, tracing.
`role` is the NATS role user — `core` for core-app, `module` for a module.
*/}}
{{- define "epicurus.commonEnv" -}}
{{- $ctx := .ctx -}}
- name: APP_ENV
  value: {{ $ctx.Values.appEnv | quote }}
- name: LOG_LEVEL
  value: {{ $ctx.Values.logLevel | quote }}
- name: DEFAULT_TENANT_ID
  value: {{ $ctx.Values.defaultTenantId | quote }}
- name: NATS_URL
  value: {{ include "epicurus.natsUrl" $ctx | quote }}
- name: NATS_USER
  value: {{ .role | quote }}
- name: NATS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "epicurus.secretName" $ctx }}
      key: {{ if eq .role "core" }}NATS_CORE_PASSWORD{{ else }}NATS_MODULE_PASSWORD{{ end }}
- name: OTEL_TRACES_ENABLED
  value: {{ $ctx.Values.tracing.enabled | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ $ctx.Values.tracing.endpoint | quote }}
{{- end -}}

{{/*
The OpenBao app token, mounted as a FILE rather than an env var.

Where it comes from depends on who runs OpenBao. In-chart: the bootstrap Job
writes it into its own Secret through the Kubernetes API, so the Secret may not
exist yet on a first install — the volume is `optional` and an init container
blocks until the file lands (the kubelet syncs it in without a restart). External:
the operator supplies the Secret themselves (`openbao.external.tokenSecret`), so
it must exist and there is nothing to wait for.

`epicurus.openbaoTokenSecret` renders empty when no token file is in play at all,
which is the flag every template branches on. CoreSettings.resolve_openbao_token
re-reads the file on every re-authentication, so a rotation needs no restart.
*/}}
{{- define "epicurus.openbaoTokenSecret" -}}
{{- if .Values.openbao.enabled -}}
{{- if .Values.openbao.bootstrap.enabled -}}{{ .Values.openbao.bootstrap.secretName }}{{- end -}}
{{- else -}}
{{- .Values.openbao.external.tokenSecret -}}
{{- end -}}
{{- end -}}

{{- define "epicurus.openbaoTokenKey" -}}
{{- if .Values.openbao.enabled -}}app-token{{- else -}}{{ .Values.openbao.external.tokenSecretKey }}{{- end -}}
{{- end -}}

{{- define "epicurus.openbaoTokenVolume" -}}
- name: openbao-token
  secret:
    secretName: {{ include "epicurus.openbaoTokenSecret" . }}
    optional: {{ .Values.openbao.enabled }}
    items:
      - key: {{ include "epicurus.openbaoTokenKey" . }}
        path: openbao-token
{{- end -}}

{{- define "epicurus.openbaoTokenMount" -}}
- name: openbao-token
  mountPath: /var/run/secrets/epicurus
  readOnly: true
{{- end -}}

{{- define "epicurus.waitForOpenbaoToken" -}}
- name: wait-for-openbao-token
  image: {{ include "epicurus.thirdPartyImage" .Values.openbao.bootstrap.image }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - /bin/sh
    - -c
    - |
      n=0
      while [ ! -s /var/run/secrets/epicurus/openbao-token ]; do
        if [ $((n % 12)) -eq 0 ]; then
          echo "waiting for the OpenBao app token from the bootstrap job (${n}0s)..."
        fi
        n=$((n + 1))
        sleep 10
      done
      echo "token present."
  volumeMounts:
    {{- include "epicurus.openbaoTokenMount" . | nindent 4 }}
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]
{{- end -}}
