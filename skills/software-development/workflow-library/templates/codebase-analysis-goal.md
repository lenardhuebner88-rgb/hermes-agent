# Codebase Analysis Goal

```text
/goal Analyze <repo-path> read-only. Inspect README, project structure, dependency/config files, central modules, and representative tests. Do not write files, restart services, run production smoke traffic, or print secrets. Deliver: executive summary, file/command-backed facts, architecture map, top 5 risks, top 5 quick wins, larger modernization initiatives, recommended order, open questions, and residual risk for areas not inspected. Done only after the evidence sources are named.
```

## Done checklist

- [ ] No writes or runtime mutations happened.
- [ ] Evidence includes real file paths and/or command output.
- [ ] Findings separate facts, hypotheses, risks, and next actions.
- [ ] Residual risk covers uninspected subsystems.
