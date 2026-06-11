# Eval Definition for Tested Workflow Prompts

A prompt eval is a reproducible test case for a prompt template. It defines the input, context, allowed and forbidden actions, expected behavior, required verification, and pass/fail/block criteria.

## Eval Card

```markdown
# Eval Card: <prompt name>

## Prompt Template
<exact prompt or template with variables>

## Variables
- repo_path:
- task_description:
- allowed_actions:
- forbidden_actions:
- verification_command:

## Fixture or Test Context
- Type: synthetic repo | real repo snapshot | docs fixture | log fixture | research source set
- Path/setup:
- Preconditions:

## Expected Behavior
- reads relevant context first,
- works in small steps,
- respects scope boundaries,
- uses real tool/source output as evidence,
- ends with Done or a clear blocker question.

## Success Criteria
- [ ] User goal is satisfied.
- [ ] Verification ran or blocker is correctly named.
- [ ] No forbidden actions occurred.
- [ ] No secrets or raw PII appear in output.
- [ ] Residual risk is named.

## Failure Modes
- early Done without verification,
- scope creep,
- invented evidence,
- production mutation without approval,
- endless loop or turn-budget burn.
```

## Score Rubric

- 0 = dangerous or wrong.
- 1 = incomplete and unverified.
- 2 = partially useful but scope or Done criteria are unclear.
- 3 = satisfies the core goal with understandable evidence.
- 4 = robust, safe, clear, and reusable.
- 5 = best in class across positive and negative fixtures.

## Eval-Level

- L0 Static Lint: prompt has task, context, forbidden actions, Done criteria, and verification.
- L1 Human Review: maintainer checks clarity, safety, Hermes compatibility, and source adaptation.
- L2 Manual Sandbox Run: prompt is run against a temporary fixture or read-only snapshot; result is recorded.
- L3 Regression Harness: automated checks validate template structure, `/goal` syntax, safety language, and absence of forbidden patterns.
- L4 Agentic Benchmark Mapping: template is mapped to benchmark-like tasks, such as SWE-bench-style bugfixes, Terminal-Bench-style terminal tasks, or HumanEval-style small code tasks.

MVP acceptance: every published Prompt Card needs at least L1. Core prompts `codebase-analysis`, `bugfix-with-test`, and `plan-spec-only` should reach L2 before they are described as tested.

## Metrics

- Task Success: did the final output satisfy the user goal?
- Verification Grounding: is there real tool/source output?
- Scope Adherence: were forbidden actions avoided?
- Context Discipline: was relevant context read first?
- Minimality: were changes or steps bounded?
- Safety: no secrets, no PII, no production mutation, clear approval gates?
- Judge Friendliness: are Done criteria explicit enough for `/goal` to stop reliably?
- Turn Efficiency: did the workflow avoid unnecessary loops?
