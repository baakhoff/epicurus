# Remote access & hardening

epicurus is **private by default**: every published port binds to `BIND_ADDRESS`
(default `127.0.0.1`), so a fresh install is reachable only from the box it runs on.
Using the PWA from your phone is a first-class use case, though — so at some point you
will want to reach the stack from *outside* that box. This page is the concrete "how",
in the order we recommend trying it.

## Read this first — what "exposing the stack" actually exposes

The edge gateway **only routes; it does not authenticate** (ADR-0008), and neither
`core-app` nor the web shell carries any auth of its own. The web UI is served at the
gateway root and its nginx **proxies `/platform/v1/*` straight to `core-app`** — agent
runs, the Files browser, OAuth connect flows, model and provider-key settings. So the
moment the web entrypoint is reachable off-box, **everything the UI can do is reachable
by anyone who can reach it.** There is no login screen behind it yet (operator identity
is the Phase-5 Identity component).

Two rules follow, and the recipes below all obey them:

1. **A perimeter that authenticates is mandatory for any non-loopback exposure** — not a
   nice-to-have. Basic-auth or an IdP in front is the *only* thing standing between the
   internet and `/platform/v1/*`.
2. **Expose only the web entrypoint** (the gateway on `:80` → the web UI). Never publish
   or reverse-proxy the internal module ports, `core-app` (`:8082`), the platform API
   directly, or the data-plane services. The module↔core contract is **local-only by
   design** (constraint #7) — keep it on the internal Docker network.

Keep `BIND_ADDRESS=127.0.0.1` and let the perimeter be the *only* process listening on a
public interface. You almost never need `BIND_ADDRESS=0.0.0.0`.

## Option A — Tailscale (recommended)

Zero exposed ports, no certificates to manage, no firewall holes. A device-level VPN
(WireGuard under the hood) puts the box on a private tailnet that only your own devices
join — the sane default for a personal server, and what the maintainer runs.

Leave `BIND_ADDRESS` at its `127.0.0.1` default, install Tailscale on the box, then serve
the loopback-bound gateway onto your tailnet with automatic HTTPS:

```bash
tailscale up
# Put the (loopback-only) gateway on your tailnet at https://<machine>.<tailnet>.ts.net/
tailscale serve --bg 8088
```

`tailscale serve` terminates TLS with a real MagicDNS certificate and proxies to
`http://127.0.0.1:8088`. The stack itself never opens a port to the LAN or the internet —
only tailnet devices can reach it, each authenticated by Tailscale. Add your phone to the
tailnet and the PWA just works, on the go, over HTTPS.

> `tailscale funnel` can publish a serve target to the **public** internet. Only combine
> it with one of the authenticating perimeters below — on its own it would expose
> `/platform/v1/*` to everyone, exactly the exposure warned about at the top of this page.

## Option B — Reverse proxy with basic auth

When you want a normal `https://assistant.example.com` on your own domain, put a small
reverse proxy in front that terminates TLS and enforces HTTP basic auth. The stack stays
loopback-bound; the proxy is the **one** process on a public port, and it joins the
internal `epicurus` network to reach the gateway — so you don't publish the gateway port
at all.

This is an **operator-provided** perimeter, deliberately **not** part of the default
stack (constraint #7). Two files, dropped next to your `compose.yaml`:

`Caddyfile` — [Caddy](https://caddyserver.com) auto-provisions and renews the TLS
certificate, so this is the whole config:

```caddyfile
# Replace the domain and the credentials. DNS for the domain must point at this box,
# and ports 80 + 443 must reach it (for the ACME challenge and for traffic).
assistant.example.com {
	# Require a login before ANY request reaches epicurus — the only gate in front
	# of /platform/v1/*. Generate a bcrypt hash with:
	#   docker run --rm caddy:2 caddy hash-password --plaintext 'a-long-passphrase'
	basic_auth {
		you $2a$14$REPLACE_WITH_YOUR_OWN_BCRYPT_HASH_000000000000000000000000
	}

	# Forward to the internal gateway; Traefik's catch-all routes it to the web UI.
	reverse_proxy gateway:80
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

nginx is a fine substitute if you already run it — point a `proxy_pass http://gateway:80;`
`server` block at the same upstream, add `auth_basic` + an `htpasswd` file, and terminate
TLS with your own certificate (or [Certbot](https://certbot.eff.org/)). The shape is
identical: TLS + auth at the edge, `gateway:80` upstream, nothing else published.

## Option C — oauth2-proxy / OIDC (for the ambitious)

For real single sign-on — log in with Google/GitHub/your own Keycloak instead of a shared
password — put [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) in front,
still with TLS terminated ahead of it (Caddy from Option B, or your existing ingress):

```yaml
# Sketch — see the oauth2-proxy docs for the full provider setup.
services:
  auth:
    image: quay.io/oauth2-proxy/oauth2-proxy:v7.7.1
    restart: unless-stopped
    command:
      - --http-address=0.0.0.0:4180
      - --reverse-proxy=true
      - --upstream=http://gateway:80          # epicurus, behind the login
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

Front `auth:4180` with the Option-B Caddy service (swap its `reverse_proxy gateway:80`
for `reverse_proxy auth:4180`) so TLS and the login both live at the edge. This is the
most work and the most robust — pick it when more than one person uses the box, or when a
shared password isn't good enough.

## Self-hosting security checklist

Before (and after) you expose anything:

- **Keep `BIND_ADDRESS=127.0.0.1`** unless a perimeter is genuinely in front — see
  [Configuration](../user/configuration.md). Let the perimeter own the only public port.
- **Never expose the Traefik dashboard** (`:8089`). It is intentionally unauthenticated
  (`--api.insecure=true`) and loopback-bound; a reverse proxy should forward the gateway's
  **web** entrypoint (`:80`) only, never the dashboard.
- **Never proxy the internal contract.** Only the web entrypoint goes through the
  perimeter — not module ports, not `core-app`, not the data plane (constraint #7).
- **Terminate TLS** on anything that leaves the box (Options A–C all do). Plain HTTP over
  a LAN still leaks session traffic.
- **Default-deny inbound at the host firewall**, then open only what the perimeter needs
  (443, plus 80 for ACME). Tailscale (Option A) needs no inbound rules at all.
- **Store the OpenBao unseal key off-box**, not on the server whose secrets it unlocks —
  see [Secrets](secrets.md) and [Backup and restore](backup-and-restore.md), which also
  covers keeping volume snapshots somewhere other than the box.
- **Keep the host and images patched.** [Auto-deploy](auto-deploy.md) rolls released
  images onto the box; keep the OS and Docker current too.
- **Rotate provider API keys** if a box is ever exposed without a perimeter, even briefly.

## See also

- [`infra/edge/README.md`](../../infra/edge/README.md) — the gateway and the short version
  of "access is yours to control" (ADR-0008).
- [Installation → Default ports](../user/installation.md#default-ports) — where
  `BIND_ADDRESS` is introduced.
- [Configuration](../user/configuration.md) — `BIND_ADDRESS` and the other env knobs.
