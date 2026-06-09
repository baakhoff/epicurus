# User Guide

This guide is for running epicurus on your own machine or server.

epicurus runs as a set of Docker containers: a core service plus the modules and
backing services it needs. It is not exposed to the public internet by default —
you choose how to reach it (locally, over your LAN, behind a VPN, or however you
expose your own server).

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
- **Private.** Not exposed to the public internet by default; you decide how to
  reach it (local, LAN, VPN, or your own server).
- **Modular.** You add capabilities by running additional module containers
  alongside the core.
