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

- **Tailscale** — e.g. `tailscale serve` proxies the loopback-bound gateway port
  onto your tailnet (what the maintainer uses).
- **A VPN / LAN binding** — set `BIND_ADDRESS` to a specific private interface
  (it defaults to loopback) and route over your VPN.
- **A reverse proxy** (Caddy/nginx) for TLS + your own rules.
- **An auth proxy / IdP** — put **Keycloak**, oauth2-proxy, Authelia, etc. in front
  to require login before traffic reaches the gateway.

epicurus neither requires nor provides any of these by default. (This is separate
from epicurus's own user/sub-user identity — the Identity component, Phase 5.)

## Routing a new module

A module's compose fragment opts in with labels (the service-template adds these):

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.<name>.rule=Host(`<name>.localhost`)
  - traefik.http.routers.<name>.entrypoints=web
  - traefik.http.services.<name>.loadbalancer.server.port=8080
```
