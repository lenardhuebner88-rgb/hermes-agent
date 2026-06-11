# Debugging Goal

```text
/goal Analyze read-only why <symptom> happens in <repo/system>. Gather evidence from logs, code, config, and recent error output. Separate facts, hypotheses, and unknowns. Do not write files, restart services, run traffic smokes, change config, or print secrets. Deliver: problem statement, evidence, most likely root cause, alternatives considered, risk, next safe action, and one concrete approval question if verification needs mutation. Done only when every claim is tied to evidence or labelled as a hypothesis.
```

## Done checklist

- [ ] Read-only boundary held.
- [ ] Evidence is cited.
- [ ] At least one alternative hypothesis is considered.
- [ ] Next action is safe or explicitly approval-gated.
