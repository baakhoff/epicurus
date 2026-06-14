<!-- Keep this concise; delete sections that do not apply. See .workspace/AGENTS.md for the full contract. -->

## Summary

<!-- What this PR does and why, in a few lines. -->

## Linked issue

<!-- Link the tracking issue so the board card moves to Done on merge. -->
Closes #

## Version bump

<!-- The target SemVer bump for each component this PR touches, per ADR-0017
(docs/developer/versioning.md):
- MAJOR (0.x -> 1.0.0): a brand-new / unseen capability, a rewrite, or something big;
- MINOR (0.1.0 -> 0.2.0): a new user-visible capability in that component;
- PATCH (0.1.0 -> 0.1.1): a bug fix or internal change the user cannot see.
State it per component (e.g. "mail MINOR; epicurus-core PATCH"), or
"None - process/docs" if no shippable code changed. The reviewer enforces it. -->

-

## Checklist

- [ ] Tests cover the change (unit + integration; contract, edges, failure modes)
- [ ] `task check` is green (ruff, mypy `--strict`, pytest)
- [ ] `task smoke` is green if this touches the running stack (module / compose / infra)
- [ ] Docs updated in this PR — the block's page, the reference, and the nav (ADR-0013)
- [ ] `tenant_id` threaded through every scoping / metering path it touches
- [ ] Secrets via OpenBao only; nothing secret in env, logs, or git
- [ ] No AI / assistant / vendor attribution anywhere (commits, code, docs)
