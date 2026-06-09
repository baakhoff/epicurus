# Contributing

Contributions are welcome — bug fixes, new modules, documentation.

## Workflow

1. **Find or open an issue** describing the change.
2. **Branch** from `main`: `feat/<area>-<short-desc>`, `fix/<area>-<desc>`, or
   `chore/<desc>`.
3. **Make the change**, with **tests** and updated docs.
4. **Run the gates** locally — see [Testing](testing.md). They must pass.
5. **Open a pull request** that links the issue (`Closes #123`). CI runs the
   gates again; a maintainer reviews and merges.

`main` changes only through reviewed, merged pull requests.

## Commit messages

Use **[Conventional Commits](https://www.conventionalcommits.org/)**:

```
feat(core): add the event client
fix(infra): correct the NATS health check
docs: expand the module guide
```

Types: `feat`, `fix`, `docs`, `chore`, `test`. These drive the grouped release
notes.

## Labels

Issues and PRs are labeled by **area** (`area:core`, `area:infra`, `area:module`,
…) and **type** (`type:feat`, `type:fix`, …), and grouped into a **milestone**.

## Code style

- **Python 3.11+**, async throughout; **Pydantic v2**.
- **ruff** (line length 100) for lint and formatting.
- **mypy `--strict`** — keep the public API fully typed.
- One responsibility per service; cross-service contracts are MCP or NATS, never
  a shared database.

## Security

- **Never commit secrets.** Credentials live in OpenBao; a gitleaks scan runs in
  CI.
- Report a security issue privately rather than in a public issue.
