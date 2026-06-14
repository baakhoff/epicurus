---
name: Module / feature (dispatch brief)
about: Buildable work, specified so an agent can pick it up cold and reach a mergeable PR.
title: ""
labels: []
---

<!--
Write this so a build-agent can start COLD — with no shared context — and reach a
mergeable PR without coming back to ask. Keep every section concrete and delete the
guidance comments as you fill them in. See .workspace/AGENTS.md for the full contract.
-->

## Goal & surface
<!-- What it does, and the surface it exposes to the rest of the platform:
MCP tools, HTTP endpoints, UI sections/actions, NATS events. -->

## Binding decisions
<!-- The ADRs and constraints this must honour. ALWAYS:
- secrets in OpenBao, tenant-scoped (never env/git);
- all LLM/embedding access via the core (PlatformClient) — modules hold no model keys;
- thread tenant_id through every scoping/metering path;
- never mention AI/Claude anywhere in the repo.
Add the domain-specific ADRs (e.g. ADR-0016 — integration modules are domain-first
and provider-pluggable, not provider-locked). -->

## Build on (contract surfaces)
<!-- The existing pieces to use rather than reinvent:
- scaffold from templates/service-template (auto-wires the contract);
- register the module URL in core module_urls + add the fragment to root compose.yaml;
- specific endpoints/clients, e.g. GET /platform/v1/oauth/{provider}/token for credentials,
  PlatformClient.embed/chat for the LLM gateway. -->

## Sequencing
<!-- What must merge before this can start (and why), and what depends on this. -->

## Definition of done
<!-- Acceptance criteria + the merge bar:
- meaningful tests (unit + integration; cover the contract, edges, failure modes);
- docs updated in the same PR — a behaviour change without a docs change is rejected (ADR-0013);
- `task smoke` / the runtime-smoke CI gate is green. -->

## Version bump
<!-- The target SemVer bump for each component this work touches, per ADR-0017
(docs/developer/versioning.md): MAJOR (a brand-new/unseen capability or rewrite),
MINOR (a new user-visible capability), PATCH (a fix or invisible internal change).
State it per component — e.g. a new module starts at its own 0.1.0; an added
user-visible tool on an existing module is a MINOR for that module. The PR repeats
this and the reviewer enforces it. -->
