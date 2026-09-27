# Edge gateway

[Traefik](https://traefik.io) routes the stack behind a single entry point. It
discovers services by Docker label, so an installed module is routed automatically
(its compose fragment carries the labels; the service-template includes them).

By default it routes:

| Host | → service |
| --- | --- |
| `echo.localhost` | echo |
| `grafana.localhost` | Grafana |

(`*.localhost` resolves to your machine in most browsers — no hosts-file edits.)
The gateway listens on `${EDGE_HTTP_PORT:-8088}` and the unauthenticated dashboard
on `${EDGE_DASHBOARD_PORT:-8089}` — both bound to `${BIND_ADDRESS:-127.0.0.1}`, so
they are reachable only from this machine until you opt in. Override the ports and
bind address in the root `.env`.

## Access is yours to control (ADR-0008)

**The gateway only routes — it does not control access.** No authentication is
baked in and no ingress is assumed. You decide how to reach and protect this entry
point; layer your choice **in front** of the gateway:

- **Tailscale** — e.g. `tailscale serve` proxies a loopback-bound port onto your
  tailnet (what the maintainer uses) — the web shell's `8084`, not this gateway's
  `8088`, when anyone but you can reach it (see below).
- **A VPN / LAN binding** — set `BIND_ADDRESS` to a specific private interface
  (it defaults to loopback) and route over your VPN.
- **A reverse proxy** (Caddy/nginx) for TLS + your own rules.
- **An auth proxy / IdP** — put **Keycloak**, oauth2-proxy, Authelia, etc. in front
  to require login before traffic reaches the gateway.

epicurus neither requires nor provides any of these by default.

**The gateway still does not authenticate — the app now can.** epicurus has built-in
sign-in with an OpenID Connect provider (off by default; see
[Sign-in](../../docs/infrastructure/sign-in.md)). It guards requests that reach the core
through the web shell — and through this gateway's `core-app.localhost` route — but **not**
the gateway's other `Host` routes (`echo.localhost`, `mail.localhost`, `grafana.localhost`,
…), which go straight to services with no sign-in of their own. So when you put a perimeter
in front, point it at the web shell (`web:8080`, or its published port `8084`) rather than
at this gateway, and keep `BIND_ADDRESS` on loopback. The gateway stays the handy local
front door for `*.localhost` on the box itself.

## Routing a new module

A module's compose fragment opts in with labels (the service-template adds these):

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.<name>.rule=Host(`<name>.localhost`)
  - traefik.http.routers.<name>.entrypoints=web
  - traefik.http.services.<name>.loadbalancer.server.port=8080
```
