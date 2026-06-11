# Hermes Goal-Prompt-Generator

Use this to turn a concrete task into a robust Hermes `/goal` prompt.

## 1. Task type
Choose one: coding | debugging | research | docs | ops-readonly | planning | review.

## 2. Goal in one sentence
I want Hermes to ...

## 3. Context sources
- Repo/path:
- Relevant files/docs:
- Previous errors/logs:
- External sources allowed? yes/no; if yes, name domains or source classes:

## 4. Allowed actions
Hermes may:
- Read:
- Write:
- Run tests/commands:
- Use network:

## 5. Forbidden actions
Hermes must not:
- Restart services without explicit approval.
- Print secrets or raw PII.
- Change production data.
- Perform broad refactors outside scope.
- Start long/broad tests without stating cost/risk first.

## 6. Done criteria
Hermes may stop only when:
- [ ] The concrete deliverable exists.
- [ ] The narrowest useful verification ran with real output, or a blocker is named.
- [ ] Changed files or evidence sources are listed.
- [ ] Residual risk is named.
- [ ] Any blocker is a specific decision question.

## 7. Final prompt

```text
/goal <task>. First read the relevant context, then work in the smallest safe steps, then verify with <command/source check>. Allowed: <allowed actions>. Forbidden: <forbidden actions>. Done only when <done criteria>; otherwise stop with one concrete blocker question and residual risk.
```
