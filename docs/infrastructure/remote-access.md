# Remote access & hardening

epicurus is **private by default**: every published port binds to `BIND_ADDRESS`
(default `127.0.0.1`), so a fresh install is reachable only from the box it runs on.
Using the PWA from your phone is a first-class use case, though — so at some point you
will want to reach the stack from *outside* that box. This page is the concrete "how",
in the order we recommend trying it.

## Read this first — what "exposing the stack" actually exposes

The web shell's nginx **proxies `/platform/v1/*` straight to `core-app`** — agent runs,
the Files browser, OAuth connect flows, model and provider-key settings. So the moment the
web shell is reachable off-box, **everything the UI can do is reachable by anyone who can
reach it — unless sign-in is on.**

epicurus has **built-in sign-in** with an OpenID Connect provider (Pocket ID, Authentik,
Keycloak, Authelia, Kanidm, Google — see [Sign-in](sign-in.md), #969). It is **off by
default**: a stack that sets nothing has no login screen, exactly as before. The edge gateway
still **only routes; it does not authenticate** (ADR-0008) — and sign-in covers requests that
reach the core *through the web shell*, not the gateway's per-module `<name>.localhost` routes
or the published internal ports.

Three rules follow, and the recipes below all obey them:

1. **Something must authenticate for any non-loopback exposure** — not a nice-to-have.
   Built-in sign-in, basic-auth at a reverse proxy, or an IdP-aware proxy: one of them is
   the only thing standing between the network and `/platform/v1/*`. Tailscale's device
   authentication counts for a tailnet only you are on.
2. **Expose only the web shell** — `web:8080` on the internal network, or its published
   port `8084`. **Not the gateway**: it also routes `echo.localhost`, `mail.localhost`,
   `storage.localhost`, `grafana.localhost` and every other module by `Host` header, and
   those routes are outside sign-in. Never publish or reverse-proxy the module ports,
   `core-app` (`:8082`), the platform API directly, or the data-plane services. The
   module↔core contract is **local-only by design** (constraint #7).
3. **Sign-in and a network perimeter compose.** The perimeter decides which devices can
   reach the box; sign-in decides which people get in. For a stack you use from your
   phone, run both.

Keep `BIND_ADDRESS=127.0.0.1` and let the perimeter be the *only* process listening on a
public interface. You almost never need `BIND_ADDRESS=0.0.0.0`.

## Option A — Tailscale (recommended)

Zero exposed ports, no certificates to manage, no firewall holes. A device-level VPN
(WireGuard under the hood) puts the box on a private tailnet that only your own devices
join — the sane default for a personal server, and what the maintainer runs.

Leave `BIND_ADDRESS` at its `127.0.0.1` default, install Tailscale on the box, then serve
the loopback-bound web shell onto your tailnet with automatic HTTPS:

```bash
tailscale up
# Put the (loopback-only) web shell on your tailnet at https://<machine>.<tailnet>.ts.net/
tailscale serve --bg 8084
```

`tailscale serve` terminates TLS with a real MagicDNS certificate and proxies to
`http://127.0.0.1:8084` — the web shell's published port (`WEB_PORT`), not the gateway's
`8088`, so the gateway's per-module routes stay on the box. The stack itself never opens
a port to the LAN or the internet — only tailnet devices can reach it, each authenticated
by Tailscale. Add your phone to the tailnet and the PWA just works, on the go, over HTTPS.

If more than one person is on the tailnet, or you want a login on the phone anyway, turn on
[built-in sign-in](#option-b--built-in-sign-in-oidc) too, with
`OAUTH_REDIRECT_BASE_URL=https://<machine>.<tailnet>.ts.net`.

> `tailscale funnel` can publish a serve target to the **public** internet. Only combine
> it with sign-in or one of the authenticating perimeters below — on its own it would
> expose `/platform/v1/*` to everyone, exactly the exposure warned about at the top of this
> page.

## Option B — Built-in sign-in (OIDC)

Recommended whenever you use the stack from several devices or your phone. Point epicurus at
the OpenID Connect provider you already run — or install [Pocket ID](https://pocket-id.org),
a small passkey-only one — and the web shell shows **Sign in with …** until you have a
session. It is native: the phone PWA signs in with a passkey and stays signed in (a sliding
30 days by default), instead of a browser basic-auth prompt, and it works the same on Compose
and on Kubernetes.

```dotenv
# .env — the full walkthrough is in the Sign-in guide
OAUTH_REDIRECT_BASE_URL=https://assistant.example.com
AUTH_MODE=oidc
OIDC_ISSUER_URL=https://id.example.com
OIDC_CLIENT_ID=<from the provider>
OIDC_CLIENT_SECRET=<from the provider; blank for a public client>
OIDC_ALLOWED_EMAILS=you@example.com
```

Sign-in is not a perimeter: it still needs TLS in front (Tailscale, or Caddy from Option C
without its `basic_auth` block), and it covers the web shell only — so the rules above about
exposing `web` and nothing else still apply. See [Sign-in](sign-in.md) for the provider setup,
Kubernetes values, who is admitted, and troubleshooting.

## Option C — Reverse proxy with basic auth

When you want a normal `https://assistant.example.com` on your own domain, put a small
reverse proxy in front that terminates TLS and enforces HTTP basic auth. The stack stays
loopback-bound; the proxy is the **one** process on a public port, and it joins the
internal `epicurus` network to reach the web shell — so you don't publish any epicurus port
at all.

This is an **operator-provided** perimeter, deliberately **not** part of the default
stack (constraint #7). Two files, dropped next to your `compose.yaml`:

`Caddyfile` — [Caddy](https://caddyserver.com) auto-provisions and renews the TLS
certificate, so this is the whole config:

```caddyfile
# Replace the domain and the credentials. DNS for the domain must point at this box,
# and ports 80 + 443 must reach it (for the ACME challenge and for traffic).
assistant.example.com {
	# Require a login before ANY request reaches epicurus. Drop this block if you use
	# built-in sign-in (Option B) instead. Generate a bcrypt hash with:
	#   docker run --rm caddy:2 caddy hash-password --plaintext 'a-long-passphrase'
	basic_auth {
		you $2a$14$REPLACE_WITH_YOUR_OWN_BCRYPT_HASH_000000000000000000000000
	}

	# Forward to the web shell — not the gateway, whose per-module Host routes sit
	# outside any login.
	reverse_proxy web:8080
}
```

`perimeter.yaml` — a standalone compose file that attaches to the network the main stack
already created and publishes only 443 (plus 80 for the ACME challenge / HTTPS redirect):

```yaml
# Start AFTER the main stack is up:  docker compose -f perimeter.yaml up -d
# It is intentionally separate from the epicurus stack — a deliberate, gated capability.
services:
  perimeter:
    image: caddy:2
    restart: unless-stopped
    # The ONLY deliberately public ports. Everything else stays on BIND_ADDRESS (loopback).
    # Set PERIMETER_BIND to a specific interface to narrow it further (e.g. a VPN address).
    ports:
      - "${PERIMETER_BIND:-0.0.0.0}:443:443"
      - "${PERIMETER_BIND:-0.0.0.0}:80:80"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data      # certificates + keys — a named volume, NEVER a host home dir
      - caddy-config:/config
    networks: [epicurus]
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  caddy-data:
  caddy-config:

networks:
  epicurus:
    # Attach to the network the main stack created (infra/compose defines `name: epicurus`).
    external: true
    name: epicurus
```

nginx is a fine substitute if you already run it — point a `proxy_pass http://web:8080;`
`server` block at the same upstream, add `auth_basic` + an `htpasswd` file (or rely on
built-in sign-in), and terminate TLS with your own certificate (or
[Certbot](https://certbot.eff.org/)). The shape is identical: TLS + auth at the edge,
`web:8080` upstream, nothing else published.

## Option D — oauth2-proxy in front

[Built-in sign-in](#option-b--built-in-sign-in-oidc) now covers what this option was for —
single sign-on with your own provider — natively, including on the phone PWA. An
[oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) in front remains an option when
you already run one for every app on the box, still with TLS terminated ahead of it (Caddy
from Option C, or your existing ingress):

```yaml
# Sketch — see the oauth2-proxy docs for the full provider setup.
services:
  auth:
    image: quay.io/oauth2-proxy/oauth2-proxy:v7.7.1
    restart: unless-stopped
    command:
      - --http-address=0.0.0.0:4180
      - --reverse-proxy=true
      - --upstream=http://web:8080            # epicurus's web shell, behind the login
      - --provider=oidc                        # or google, github, …
      - --oidc-issuer-url=https://your-idp.example.com/
      - --email-domain=example.com             # who is allowed in
      # --client-id / --client-secret / --cookie-secret via env or a secrets file
    networks: [epicurus]
networks:
  epicurus:
    external: true
    name: epicurus
```

Front `auth:4180` with the Option-C Caddy service (swap its `reverse_proxy web:8080`
for `reverse_proxy auth:4180`) so TLS and the login both live at the edge. The app then
still has no idea who is signed in, and the phone PWA meets the proxy's login page rather
than its own sign-in screen — which is why built-in sign-in is now the recommended route.

## Self-hosting security checklist

Before (and after) you expose anything:

- **Keep `BIND_ADDRESS=127.0.0.1`** unless a perimeter is genuinely in front — see
  [Configuration](../user/configuration.md). Let the perimeter own the only public port.
  Sign-in does not cover the published ports (`core-app`'s `8082`, each module's), so this
  holds with sign-in on too.
- **Turn on [sign-in](sign-in.md)** for anything reached from more than this box, with an
  explicit allowlist (`OIDC_ALLOWED_EMAILS` / `OIDC_ALLOWED_GROUPS`) — never
  `OIDC_ALLOW_ALL_USERS=true` against a public provider such as Google.
- **Point the perimeter at the web shell** (`web:8080` / port `8084`), never at the gateway
  (`8088`): its `<module>.localhost` routes bypass sign-in.
- **Never expose the Traefik dashboard** (`:8089`). It is intentionally unauthenticated
  (`--api.insecure=true`) and loopback-bound; a reverse proxy forwards the web shell only,
  never the dashboard.
- **Never proxy the internal contract.** Only the web shell goes through the
  perimeter — not module ports, not `core-app`, not the data plane (constraint #7).
- **Terminate TLS** on anything that leaves the box (Options A, C and D do; B needs one of
  them). Plain HTTP over a LAN still leaks session traffic — and a sign-in session cookie is
  marked `Secure` only on an https address.
- **Default-deny inbound at the host firewall**, then open only what the perimeter needs
  (443, plus 80 for ACME). Tailscale (Option A) needs no inbound rules at all.
- **Store the OpenBao unseal key off-box**, not on the server whose secrets it unlocks —
  see [Secrets](secrets.md) and [Backup and restore](backup-and-restore.md), which also
  covers keeping volume snapshots somewhere other than the box.
- **Keep the host and images patched.** [Auto-deploy](auto-deploy.md) rolls released
  images onto the box; keep the OS and Docker current too.
- **Rotate provider API keys** if a box is ever exposed without a perimeter, even briefly.
- **Keep `OIDC_CLIENT_SECRET` in `.env` or a Kubernetes Secret** — never in a committed file
  or a values file.

## See also

- [Sign-in](sign-in.md) — built-in OpenID Connect sign-in: the Pocket ID walkthrough, other
  providers, admission rules and troubleshooting.
- [`infra/edge/README.md`](../../infra/edge/README.md) — the gateway and the short version
  of "access is yours to control" (ADR-0008).
- [Installation → Default ports](../user/installation.md#default-ports) — where
  `BIND_ADDRESS` is introduced.
- [Configuration](../user/configuration.md) — `BIND_ADDRESS` and the other env knobs.
