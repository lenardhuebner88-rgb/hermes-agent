# Workflow Library Examples

## Codebase analysis

Use `templates/codebase-analysis-goal.md` when the operator asks what is in a repo, what is risky, or what should be improved first.

## Codebase modernization

Use `templates/codebase-modernization-goal.md` when the operator asks to bring a project to the latest level. Start with read-only planning and gate major upgrades.

## Bugfix with regression test

```text
/goal Fix <bug/symptom> in <repo-path>. Read the failing output, relevant tests, and implementation first. Add a regression test when practical, make the smallest code change, and verify with the narrowest test command. Stop only when the check is green or a blocker question names the exact missing input.
```

## Plan-spec only

```text
/goal Create only a plan-spec draft for <proposal>, without code or config changes. Check existing Hermes capabilities, define problem, target state, non-goals, MVP artifacts, acceptance criteria, eval/test strategy, risks, and follow-up cards. Stop when the self-contained Markdown draft is complete.
```
