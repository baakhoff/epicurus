# Sign-in (OpenID Connect)

epicurus can make you sign in before anything is shown: the core is an OpenID Connect
*relying party*, and the web shell puts a sign-in screen in front of the app until you have a
session. You bring the identity provider — [Pocket ID](https://pocket-id.org) (passkeys only),
Authentik, Keycloak, Authelia, Kanidm, or Google — and the same configuration works on the
Docker Compose stack and the Helm chart, on the desktop and in the installed phone app (#969).

It is **off by default**. A deployment that sets nothing behaves exactly as it did before
sign-in existed: whoever reaches the web shell reaches everything. Turn it on when you reach
the stack from more than the machine it runs on — several devices, your phone, a household.

- [What sign-in covers — and what it does not](#what-sign-in-covers--and-what-it-does-not)
- [Set it up with Pocket ID](#set-it-up-with-pocket-id)
- [Configuration](#configuration)
- [Who may sign in](#who-may-sign-in)
- [Other providers](#other-providers)
- [On your phone](#on-your-phone)
- [Signing out](#signing-out)
- [Troubleshooting](#troubleshooting)

## What sign-in covers — and what it does not

The core requires a session on **every request that reached it through a proxy** — one that
carries `X-Forwarded-For`, `Forwarded`, `X-Forwarded-Host`, `X-Forwarded-Proto` or
`X-Real-IP`. That is:

- the web shell's nginx, which proxies `/platform/*` to the core (Compose and Kubernetes);
- the Kubernetes Ingress in front of the web shell;
- the Compose gateway's `core-app.localhost` route.

A signed-out request there gets **401** (`{"detail": "Sign in to continue.", "code":
"unauthenticated"}`), and the web shell turns that into the sign-in screen. A state-changing
request (`POST`, `PUT`, `PATCH`, `DELETE`) that comes from another site gets **403** even
with a valid session — the browser's `Sec-Fetch-Site` must say `same-origin` or `none`, or
its `Origin` must be the deployment's public address. That closes the gap `SameSite=Lax`
leaves open when several home-lab apps share one domain. `/health` and
`/platform/v1/auth/*` (the sign-in flow itself) are always reachable.

What it deliberately does **not** touch: a module calling the core's platform API directly on
the internal network. Those calls carry no proxy headers and need no session, which is why
turning sign-in on changes nothing in any module (the module↔core contract is local-only,
constraint #7).

That same rule decides what sign-in **cannot** protect. Anything that reaches a service
*without* going through one of the proxies above is outside it:

- **Published internal ports.** `core-app` publishes `:8082` and every module its own port.
  A direct request there carries no proxy headers, so the core treats it as internal. Keep
  `BIND_ADDRESS=127.0.0.1` (the default) so those ports answer only the box itself.
- **The Compose gateway's `<module>.localhost` routes.** The gateway (Traefik, `:8088`)
  routes `echo.localhost`, `mail.localhost`, `storage.localhost`, `grafana.localhost` and the
  rest straight to their services, which have no sign-in of their own. Anyone who can reach
  the gateway can pick one with a `Host` header. The gateway's own dashboard (`:8089`, no
  authentication) is in the same position.
- **Anything on the `epicurus` network.** A container you attach to it — a Caddy or
  oauth2-proxy of your own included — reaches `core-app:8080` directly, and a request that
  does not add a forwarding header is treated as internal. Put your perimeter in front of the
  web shell, and let it be the only thing you attach.

So on **Compose**, point your perimeter — `tailscale serve`, Caddy, nginx — at the **web
shell** (`web:8080` on the `epicurus` network, or its published port `8084`), **not** at the
gateway, and keep `BIND_ADDRESS` on loopback. [Remote access](remote-access.md) has the
recipes. On **Kubernetes** the chart's Ingress already routes to the web shell alone; the one
thing to add is [`networkPolicy.enabled`](kubernetes.md#networkpolicy) when other workloads
share the cluster, because without it any pod can call `core-app:8080` directly. The
NetworkPolicies are ingress-only, so the core can still reach your provider.

Sign-in and a network perimeter **compose**; they do not replace each other. Tailscale keeps
the internet away from the box; sign-in decides who, among the devices that can reach it, gets
in. Using both is the recommended setup for a stack you open on your phone.

## Set it up with Pocket ID

Pocket ID is a small, passkey-only provider — no passwords anywhere, which suits a personal
server well. The same steps work for any provider; [Other providers](#other-providers) lists
what differs.

### 1. Decide the public address

Sign-in works at **one** address: the provider sends the browser back to
`<public address>/platform/v1/auth/callback`, and the session cookie belongs to that host.
Pick the https address you will open on every device — e.g.
`https://assistant.example.com`, or `https://<machine>.<tailnet>.ts.net` with
`tailscale serve`. Visiting the same stack at a second hostname (a LAN IP, `localhost`) does
not work for sign-in; the flow ends in `state_mismatch` or the cookie never arrives.

- **Compose:** `OAUTH_REDIRECT_BASE_URL=https://assistant.example.com` in `.env`.
- **Kubernetes:** derived from the Ingress — `https://<ingress.host>` when
  `ingress.tls.enabled`, `http://…` otherwise — or set `core.oauth.redirectBaseUrl`.

It must be **https** (the session cookie is marked `Secure` only then, and passkeys need a
secure page anyway), and epicurus must be served at the **root** of that address, not under
a path. It is the same address the connected-account OAuth flow (Google) already uses, so the
two cannot drift.

### 2. Create the client in Pocket ID

In Pocket ID: **Administration → OIDC Clients → Add OIDC Client**.

| Field | Value |
| --- | --- |
| Name | `epicurus` (what Pocket ID shows on its consent and apps pages) |
| Callback URLs | `https://assistant.example.com/platform/v1/auth/callback` — exactly |
| PKCE | **on** (epicurus always uses PKCE with S256) |
| Public client | **off** — then copy the client secret. Or **on**, and leave `OIDC_CLIENT_SECRET` blank: PKCE alone protects the exchange. |

Save, then copy the **client ID** (and the **client secret**, for a confidential client — it
is shown once).

Then, recommended: on the client's page set **Allowed User Groups** to a group holding the
people who may use epicurus. Pocket ID then refuses everyone else before they ever reach
epicurus — and that is what makes `OIDC_ALLOW_ALL_USERS=true` a sound admission rule (see
[Who may sign in](#who-may-sign-in)).

The issuer is Pocket ID's own address, e.g. `https://id.example.com`. If you admit by group
on the epicurus side, request the `groups` scope — Pocket ID sends the `groups` claim only then.

### 3a. Configure Compose

In `.env` (next to `compose.yaml`, gitignored):

```dotenv
OAUTH_REDIRECT_BASE_URL=https://assistant.example.com
AUTH_MODE=oidc
OIDC_ISSUER_URL=https://id.example.com
OIDC_PROVIDER_NAME=Pocket ID
OIDC_CLIENT_ID=<client id from Pocket ID>
OIDC_CLIENT_SECRET=<client secret from Pocket ID; blank for a public client>
OIDC_ALLOWED_EMAILS=you@example.com
```

`OIDC_CLIENT_SECRET` is a secret. `.env` is gitignored — keep it there, and never paste it into
a committed file, a compose override you share, or an issue. Then recreate the core:

```bash
docker compose up -d core-app
```

Open `https://assistant.example.com`: the sign-in screen offers **Sign in with Pocket ID**.

### 3b. Configure Kubernetes

Keep the client credentials in a Secret, never in a values file. Create it without the
secret touching your shell history:

```bash
read -rp 'client id: ' OIDC_ID
read -rsp 'client secret: ' OIDC_SECRET; echo
kubectl -n epicurus create secret generic epicurus-oidc \
  --from-literal=OIDC_CLIENT_ID="$OIDC_ID" \
  --from-literal=OIDC_CLIENT_SECRET="$OIDC_SECRET"
unset OIDC_ID OIDC_SECRET
```

GitOps (Flux, Argo CD): render the manifest instead of applying it, and encrypt it before it
goes anywhere near git — e.g. with [SOPS](https://github.com/getsops/sops):

```bash
kubectl -n epicurus create secret generic epicurus-oidc \
  --from-literal=OIDC_CLIENT_ID="$OIDC_ID" \
  --from-literal=OIDC_CLIENT_SECRET="$OIDC_SECRET" \
  --dry-run=client -o yaml > epicurus-oidc.secret.yaml
sops --encrypt --in-place epicurus-oidc.secret.yaml
```

Then the values:

```yaml
ingress:
  enabled: true
  host: assistant.example.com
  tls:
    enabled: true
    secretName: assistant-example-com-tls
auth:
  mode: oidc
  oidc:
    issuerUrl: https://id.example.com
    providerName: Pocket ID
    existingSecret: epicurus-oidc   # OIDC_CLIENT_ID + OIDC_CLIENT_SECRET
    allowedEmails:
      - you@example.com
```

`helm upgrade` prints the callback URL to register (NOTES), derived from `ingress.host` and
`ingress.tls.enabled` — or from `core.oauth.redirectBaseUrl` if you set it.

The client id is an identifier, not a secret: `auth.oidc.clientId` may carry it in the clear
instead, and then the Secret needs only `OIDC_CLIENT_SECRET` (or nothing at all, for a public
client — the chart marks that key optional). With no `auth.oidc.existingSecret`, both keys
are read from the shared Secret (`secrets.existingSecret`); the chart-generated
`epicurus-secrets` holds no OIDC keys, so the chart refuses a release that would have to read
the client id from it. A missing `OIDC_CLIENT_ID` key in a Secret you named is not a render
error but a `CreateContainerConfigError` on the core pod. The pod reads the Secret at start,
so after rotating the client secret: `kubectl -n epicurus rollout restart deploy/core-app`.

## Configuration

The core reads these (full reference: [config](../reference/config.md); the endpoints are in
the [platform API](../reference/platform-api.md)).

| Env (Compose) | Chart value | Default | Meaning |
| --- | --- | --- | --- |
| `AUTH_MODE` | `auth.mode` | `none` | `none` = no sign-in; `oidc` = sign in with an OpenID Connect provider. |
| `OIDC_ISSUER_URL` | `auth.oidc.issuerUrl` | — | The provider's issuer. Discovery is `<issuer>/.well-known/openid-configuration`, fetched lazily — a provider that is down never blocks start-up, only sign-in. |
| `OIDC_CLIENT_ID` | `auth.oidc.clientId`, else `OIDC_CLIENT_ID` in the Secret | — | Required with `oidc`. |
| `OIDC_CLIENT_SECRET` | `OIDC_CLIENT_SECRET` in the Secret | — | Blank = a public client (PKCE only). |
| `OIDC_SCOPES` | `auth.oidc.scopes` | `openid email profile` | Space- or comma-separated; `openid` is always sent. Add `groups` for group rules. |
| `OIDC_PROVIDER_NAME` | `auth.oidc.providerName` | — | The button label, "Sign in with Pocket ID". Blank = "Sign in". |
| `OIDC_ALLOWED_EMAILS` | `auth.oidc.allowedEmails` | — | Comma-separated (the chart takes a list). |
| `OIDC_ALLOWED_GROUPS` | `auth.oidc.allowedGroups` | — | Comma-separated, matched against the `groups` claim. |
| `OIDC_ALLOW_ALL_USERS` | `auth.oidc.allowAllUsers` | `false` | Admit anyone the provider authenticates for this client. |
| `OIDC_AUTO_REDIRECT` | `auth.oidc.autoRedirect` | `false` | A signed-out visitor goes straight to the provider instead of the sign-in screen. |
| `AUTH_SESSION_DAYS` | `auth.sessionDays` | `30` | Sliding session lifetime. |
| `OAUTH_REDIRECT_BASE_URL` | `core.oauth.redirectBaseUrl`, else the Ingress | `http://localhost:8084` | The public address; the callback is `<this>/platform/v1/auth/callback`. |

**The core refuses to start** with `AUTH_MODE=oidc` and no issuer, no client id, or no
admission rule — it names everything missing in one log line. The chart refuses to *render*
the same release (so `helm install` says why instead of a crash-looping pod), and also refuses
an `auth.mode` other than `none`/`oidc` and any sign-in variable placed in `core.extraEnv`
(configure sign-in under `auth` only — a second copy would be a duplicate env entry, which
server-side apply rejects).

Sessions live on the server: an opaque token in an HttpOnly, `SameSite=Lax` cookie named
`epicurus_session` (`Secure` on an https address), stored hashed with its tenant.

## Who may sign in

Authentication is the provider's job; **admission** is epicurus's. With `oidc` you must say who
is admitted, in one or more of three ways:

- **`OIDC_ALLOWED_EMAILS`** — the people, by email. Case-insensitive. An address the provider
  marks unverified is never admitted by this rule (`email_unverified`).
- **`OIDC_ALLOWED_GROUPS`** — anyone in one of these groups, matched exactly against the ID
  token's or userinfo's `groups` claim. Needs the `groups` scope with most providers.
- **`OIDC_ALLOW_ALL_USERS=true`** — anyone the provider authenticates *for this client*. The
  allowlists are then ignored. Use it only when the provider itself restricts the client, as
  Pocket ID's **Allowed User Groups** does.

An empty allowlist is **refused, never read as "everyone"**: point that at a public provider
such as Google and anyone with a Google account would be admitted. "Everyone" has to be said
out loud, with `OIDC_ALLOW_ALL_USERS=true`.

Every admitted user has **full access**. epicurus v1 is single-tenant: everyone who signs in
sees the same chats, memory, files and connected accounts. Separate per-person spaces arrive
with workspaces (2.0.0) — until then, admit only people you would hand the box to.

## Other providers

The flow is standard (authorization code + PKCE S256), so any conforming provider works.
Register the same callback URL; what differs is the issuer's shape and how groups are sent.

| Provider | Issuer (`OIDC_ISSUER_URL`) | Groups |
| --- | --- | --- |
| **Pocket ID** | `https://id.example.com` | `groups` scope; restrict the client with Allowed User Groups. |
| **Authentik** | `https://authentik.example.com/application/o/<application slug>/` — trailing slash included; copy it from the provider's page | The default `profile` scope mapping includes `groups`. |
| **Keycloak** | `https://keycloak.example.com/realms/<realm>` (older versions: `/auth/realms/<realm>`) | Add a *Group Membership* mapper to the client, token claim name `groups`, "Full group path" off. |
| **Authelia** | `https://auth.example.com` | `groups` scope. Register the client in Authelia's `identity_providers.oidc.clients`. |
| **Kanidm** | `https://idm.example.com/oauth2/openid/<client name>` | `groups` scope; Kanidm names groups `name@domain` unless the client prefers short names — match what it sends. |
| **Google** | `https://accounts.google.com` | None. |

**Google** authenticates *any* Google account. A Google client with `OIDC_ALLOW_ALL_USERS=true`
admits the internet; set `OIDC_ALLOWED_EMAILS` to the addresses that may sign in. (The Google
client for sign-in is separate from the one the calendar/mail modules use for connected
accounts, though both return to the same public address.)

The issuer must match what the provider publishes *exactly* — scheme, host, path and trailing
slash — or ID tokens fail validation (`invalid_token`).

## On your phone

The installed PWA signs in the same way: **Sign in with …** leaves the app for the provider's
page and comes back to the callback, then to where you were. Passkeys work inside the installed
app (Pocket ID's whole point). You then stay signed in for `AUTH_SESSION_DAYS` (30 by default)
**sliding** — every use pushes the expiry out again, so a phone you use weekly never asks
again, and one left in a drawer for a month does.

With `OIDC_AUTO_REDIRECT=true` a signed-out visitor skips epicurus's sign-in screen and goes
straight to the provider; the shell guards against a redirect loop if the provider keeps
sending it back.

**A device that installed the app before this release** still runs the old offline worker,
which answers *every* navigation from its cache — the sign-in callback included — so the
provider's redirect back never reaches the core, and the old app it serves has no sign-in
screen. Accept the **"A new epicurus is ready" → Refresh** prompt on that device (it appears on
the next open) before signing in. The same update is what lets connected-account callbacks
(Google) reach the core on a device with the app installed.

## Signing out

**Settings** shows who is signed in, with **Sign out**. Signing out ends the *epicurus*
session: the server-side session is deleted and the cookie cleared. It does **not** sign you
out of the provider — that session remains, so the next **Sign in** can be instant (with Pocket
ID, one passkey touch, or none). To end everything, sign out at the provider too.

## Troubleshooting

A failed sign-in always lands back on the sign-in screen with a sentence; the address bar shows
`/?auth_error=<code>`. The core logs the detail (never a token or secret).

| `auth_error` | What happened | What to do |
| --- | --- | --- |
| `provider_unreachable` | The core could not fetch the provider's discovery document, keys, or token endpoint (network error or a 5xx). | See [the core can't reach the issuer](#the-core-cannot-reach-the-issuer) below. |
| `provider_error` | The provider answered with an error (other than a refusal) or a callback with neither a code nor an error. | Read the provider's log; usually a client setting (disabled client, wrong grant type). |
| `access_denied` | The provider refused or the user declined — e.g. not in Pocket ID's Allowed User Groups. | Add the user on the provider's side. |
| `state_mismatch` | The login transaction was missing, expired, or already used. | Start sign-in and finish it at **the same address** as `OAUTH_REDIRECT_BASE_URL` — a second hostname drops the transaction cookie. Don't reuse an old callback tab; don't take many minutes at the provider. |
| `token_exchange_failed` | The provider refused the code, or returned no ID token. | Check the client secret (or blank it for a public client), and that the provider's client uses the same public/confidential type. |
| `invalid_token` | The ID token failed validation — signature, issuer, audience, expiry or nonce. | `OIDC_ISSUER_URL` must match the provider's `issuer` exactly (trailing slash!); `OIDC_CLIENT_ID` must be this client's; check the box's clock. |
| `not_allowed` | Authenticated, but no admission rule admits this person. | Add their email or group, or restrict the client at the provider and use `OIDC_ALLOW_ALL_USERS=true`. |
| `groups_claim_missing` | A group rule is set but the provider sent no `groups` claim at all. | Add `groups` to `OIDC_SCOPES` (Pocket ID, Authelia, Kanidm) or add a groups mapper (Keycloak). |
| `email_unverified` | The email is allowed, but the provider says it is not verified. | Verify the address at the provider. |
| `misconfigured` | The provider's metadata does not fit this client, or the core could not run the flow. | Read the core's log line for the specifics. |

### The provider says "redirect_uri mismatch" (or "invalid callback URL")

The callback registered at the provider must equal
`<OAUTH_REDIRECT_BASE_URL>/platform/v1/auth/callback` character for character — scheme
(`https`), host, port, no trailing slash. On Kubernetes, copy it from the chart's NOTES. If you
open the stack at several addresses, pick one and use only that one.

### The core cannot reach the issuer

Discovery, keys and the token exchange are server-to-server calls **from the core's container**,
not from your browser. So a provider your laptop reaches can still be out of the core's reach:

- **DNS.** The core resolves the issuer's name inside the container (Docker's DNS, the cluster
  DNS). A name that exists only in your router or `/etc/hosts` may not resolve there.
- **Egress.** The core needs outbound https to the provider. The chart's NetworkPolicies are
  ingress-only, so they never block it; a cluster-wide egress policy or a firewall might.
- **A private CA.** If the provider's certificate is signed by your own CA, the core does not
  trust it by default, and every call fails as `provider_unreachable`. Serve the provider with
  a publicly trusted certificate (e.g. via Let's Encrypt), or build the core image with your CA
  in its trust store.
- **Same box, public name.** Reaching a provider on the same host by its public name can loop
  back through a router that does not support hairpin NAT; resolve it to the internal address
  inside the stack instead.

### The core does not start

`AUTH_MODE=oidc, but sign-in cannot be enabled: …` in the `core-app` log lists every problem at
once — `OIDC_ISSUER_URL is not set`, `OIDC_CLIENT_ID is not set`, `no admission rule is set …`,
an issuer or `OAUTH_REDIRECT_BASE_URL` that is not an absolute http(s) URL, or
`AUTH_SESSION_DAYS` below 1. Fix them and restart. On Kubernetes the same release does not
render in the first place; the `helm` error names the value to set. An unrecognised
`AUTH_MODE` also fails start-up — a typo in this setting must never read as "no sign-in".

### Everything returns 401 after enabling sign-in

That is sign-in working: through the proxy, nothing but `/health` and the sign-in flow answers
without a session. Sign in in the browser. A script that used `/platform/v1/*` through the web
shell now needs a session too (personal access tokens for non-browser clients are not in v1).

### Writes fail with 403

A state-changing request was treated as cross-site: the browser did not send
`Sec-Fetch-Site: same-origin`/`none`, and its `Origin` is not the public address. Use the
stack at `OAUTH_REDIRECT_BASE_URL`, not at another hostname.
