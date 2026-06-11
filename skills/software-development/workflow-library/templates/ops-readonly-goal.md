# Safe Ops Read-only Goal

```text
/goal Check read-only the state of <system/feature>. Allowed actions are only file, config, log, and status reads. Do not write files, restart services, run production traffic smokes, change config, create tasks, or print secrets/PII. Deliver facts, evidence, hypotheses, risk, and next safe action. If mutation is needed to verify, stop with a concrete approval question. Done only when all observed facts cite their evidence.
```

## Stop rules

- Stop if the only remaining verification requires a write, restart, credential, or production traffic.
- Stop if logs contain secrets; summarize without exposing raw secret values.
