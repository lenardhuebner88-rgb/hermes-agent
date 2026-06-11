# Hermes Plan-Spec-Generator

Use this to convert a raw idea into a reviewable plan-spec without implementation.

## 1. Raw idea
<one sentence, issue text, or funnel note>

## 2. Problem
What user/operator problem does this solve?

## 3. Live capability check
What already exists?
- Slash commands:
- Skills:
- CLI/gateway/tools:
- Docs:

## 4. Target state
What should users see or be able to do after implementation?

## 5. Non-goals
What is explicitly out of scope?

## 6. MVP artifacts
- Code/skill/docs/config:
- Templates:
- Tests/harness:
- Rollout/migration notes:

## 7. Acceptance criteria
- [ ] User value is covered.
- [ ] Scope boundaries are respected.
- [ ] Tests/checks are defined.
- [ ] Safety/ops risks are addressed.
- [ ] Review path is clear.

## 8. Eval and verification strategy
Which checks prove the implementation satisfies the spec?

## 9. Follow-up cards
- Implementation:
- Docs:
- Review:
- Optional later:

## 10. Final prompt

```text
/goal Create only a plan-spec draft for <idea>, without changing code or config. Check existing Hermes capabilities, then write problem, target state, non-goals, MVP artifacts, acceptance criteria, eval/test strategy, risks, and follow-up cards. Stop only when the plan-spec is a self-contained Markdown artifact with residual risk.
```
