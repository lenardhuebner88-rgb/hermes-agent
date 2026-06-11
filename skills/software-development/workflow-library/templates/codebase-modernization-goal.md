# Codebase Modernization Loop

```text
/goal Modernize <repo-path> iteratively and safely. Start with a read-only plan covering runtime versions, dependencies, lint/test setup, deprecations, and security warnings. If changes are in scope, perform only low-risk, narrowly verifiable updates. One iteration equals one change class, such as one dependency group, one lint-fix type, or one small API migration. After each change, run the narrowest relevant check. Do not perform major upgrades, broad refactors, DB migrations, service restarts, or production changes without approval. Stop with plan, executed changes, checks, and residual risks.
```

## Stop rules

- Stop before any major version upgrade unless explicitly approved.
- Stop if a check fails repeatedly with the same root cause.
- Stop if verification would require production credentials or service restarts.
