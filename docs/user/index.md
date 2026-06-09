# User Guide

This guide is for running epicurus on your own machine or server.

epicurus runs as a set of Docker containers: a core service plus the modules and
backing services it needs. You reach it privately over Tailscale — it is not
exposed to the public internet by default.

> **Early development.** There is not yet an end-user app to log into. Today this
> guide covers standing up the platform's **data plane** (its backing services).
> As the agent and web UI land, this guide gains the sections for using them.

## Sections

- **[Installation](installation.md)** — prerequisites and bringing the platform
  up.
- **[Configuration](configuration.md)** — environment configuration and where
  secrets live.

## How it's meant to run

- **Local-first.** Everything runs on your box; data stays there unless a module
  you enable explicitly needs to reach out.
- **Private.** Access is over your Tailscale network only.
- **Modular.** You add capabilities by running additional module containers
  alongside the core.
