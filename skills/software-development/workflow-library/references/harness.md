# Workflow Prompt Harness

Use this harness when adding, reviewing, or promoting a workflow prompt in the library.

## Aufnahmekriterien

A new prompt may be added when:

- It has a Prompt Card with category, sources, Stand-Datum, Hermes adaptation, Eval-Level, and risks.
- It has an Eval Card or a documented reason why only static review is possible.
- It does not require YOLO/approval bypass.
- It does not expose secrets/PII or normalize production mutation without an approval gate.
- It works as a CLI and gateway prompt.
- It uses `/goal` for actual Hermes execution and describes `/loop` only as a category unless a future command ships.

## Static Check

Review every prompt for these fields:

- Target task and output.
- Context sources to inspect first.
- Allowed actions.
- Forbidden actions.
- Narrow verification.
- Done or Stop criteria.
- Blocker question shape.
- Residual risk.

## Negative Tests

Reject or rewrite prompts that:

- use vague targets such as "improve everything" without success criteria,
- ask for broad modernization without staged verification,
- request YOLO/approval bypass,
- allow production writes, restarts, or traffic without approval,
- omit verification,
- tell users to run `/loop` as a shipped command.

## Sandbox-Fixtures

Recommended fixtures for L2/L3:

- Mini Python repo with one failing test for bugfix prompts.
- Small docs directory with stale command examples for docs prompts.
- Synthetic log/config bundle for read-only debugging prompts.
- Dummy dependency file for modernization planning prompts.
- Fixed source bundle for research prompts.

## Regression automation ideas

- Pytest static checks for required Markdown sections.
- Snapshot checks for example prompts.
- Fake runner/judge checks for `/goal` syntax and Done criteria.
- No live board, production, credential, or gateway smoke tests in the harness.

## Promotion path

- L0: static fields present.
- L1: human review accepted.
- L2: manual sandbox run recorded.
- L3: regression harness added.
- L4: benchmark-style mapping documented.
