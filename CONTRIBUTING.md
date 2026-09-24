# Contribution conventions

Keep each change focused and preserve a useful history.

- `SKILL.md` owns the runtime workflow.
- `references/contracts.md` owns exact paths, pins, formats, and values.
- `scripts/` owns executable behavior; `tests/` verifies it.
- Update `REPOSITORY_MAP.md` when files are added, removed, renamed, or given a
  different role.
- Never commit credentials, local runtime state, generated responses, npm
  dependencies, or disposable work files.

Before committing:

1. Inspect the staged diff.
2. Run the narrowest relevant tests and structural validation.
3. Scan staged content for credential material.
4. Confirm local runtime files remain untracked.

Use an imperative commit subject. For non-trivial changes, explain what
changed, why, how it was validated, and any compatibility impact.
