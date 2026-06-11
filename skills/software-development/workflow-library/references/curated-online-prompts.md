# Curated Online Prompt Patterns for Hermes Workflows

Stand-Datum der Seed-Recherche: 2026-06-11.

This file adapts public prompt-engineering and agent-evaluation patterns into Hermes-native Prompt Cards. It cites sources and extracts patterns; it does not copy external prompt text wholesale.

## Source index

| Source | URL | Library use |
|---|---|---|
| Anthropic Prompt Engineering Docs | https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview | Role, context, explicit instructions, examples, decomposition |
| OpenAI Prompt Engineering Guide | https://platform.openai.com/docs/guides/prompt-engineering | Specific instructions, reference text, task splitting |
| GitHub Copilot Prompt Engineering | https://docs.github.com/en/copilot/using-github-copilot/prompt-engineering-for-github-copilot | Coding context, repository-specific asks, examples |
| Cursor Rules Docs | https://docs.cursor.com/context/rules | Reusable project rules and context discipline |
| Aider Usage Tips | https://aider.chat/docs/usage/tips.html | Small code changes, tests, Git-aware coding loops |
| Prompting Guide ReAct | https://www.promptingguide.ai/techniques/react | Observe/act style loops for tool-supported reasoning |
| Anthropic Prompt Engineering Interactive Tutorial | https://github.com/anthropics/prompt-eng-interactive-tutorial | Didactic prompt-pattern exercises |
| SWE-bench | https://www.swebench.com/ and https://github.com/SWE-bench/SWE-bench | Realistic software-fix evaluation framing |
| Terminal-Bench | https://www.tbench.ai/ and https://github.com/laude-institute/terminal-bench | Terminal task evaluation and observable success criteria |
| SWE-agent | https://github.com/SWE-agent/SWE-agent | Agentic software-engineering trajectories |
| OpenHands | https://github.com/All-Hands-AI/OpenHands | Coding-agent sandbox and task workflow perspective |
| HumanEval | https://github.com/openai/human-eval | Small code task evaluation reference |

## Prompt Card: Codebase Analysis Goal

- Kategorie: goal | coding | review
- Quelle(n): GitHub Copilot Prompt Engineering; OpenAI Prompt Engineering Guide; Anthropic Prompt Engineering Docs
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: specific task + repository context + explicit output format + evidence requirement.
- Hermes-Adaptation:

```text
/goal Analyze <repo-path> read-only. Inspect README, project structure, dependency/config files, central modules, and representative tests. Do not write files, restart services, run production smoke traffic, or print secrets. Deliver: executive summary, file/command-backed facts, architecture map, top 5 risks, top 5 quick wins, larger modernization initiatives, recommended order, open questions, and residual risk for areas not inspected. Done only after the evidence sources are named.
```

- Eval-Level: L1 now; target L2 with a read-only repo snapshot.
- Known risks: too broad for huge monorepos; constrain paths when possible.

## Prompt Card: Codebase Modernization Loop

- Kategorie: loop | coding | maintenance
- Quelle(n): Aider Usage Tips; SWE-bench; Terminal-Bench
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: small changes, one verification per step, benchmark-style observable success.
- Hermes-Adaptation:

```text
/goal Modernize <repo-path> iteratively and safely. Start with a read-only plan covering runtime versions, dependencies, lint/test setup, deprecations, and security warnings. If changes are in scope, perform only low-risk, narrowly verifiable updates. One iteration equals one change class, such as one dependency group, one lint-fix type, or one small API migration. After each change, run the narrowest relevant check. Do not perform major upgrades, broad refactors, DB migrations, service restarts, or production changes without approval. Stop with plan, executed changes, checks, and residual risks.
```

- Eval-Level: L1 now; target L2 against a disposable fixture.
- Known risks: dependency upgrades can cause cascading failures; major upgrades need separate approval.

## Prompt Card: Bugfix With Regression Test

- Kategorie: goal | coding | debugging
- Quelle(n): SWE-bench; Aider Usage Tips; HumanEval
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: reproduce, patch minimally, test the expected behavior.
- Hermes-Adaptation:

```text
/goal Fix <bug/symptom> in <repo-path>. Read the failing output, relevant tests, and implementation first. Add a regression test when practical, make the smallest code change, and verify with the narrowest test command. Stop only when the check is green or a blocker question names the exact missing input.
```

- Eval-Level: L1 now; target L2/L3 with a mini repo fixture and static prompt checks.
- Known risks: some bugs need credentials or production data; block instead of guessing.

## Prompt Card: Read-only Incident Debugging

- Kategorie: goal | debugging | ops-readonly
- Quelle(n): Anthropic Prompt Engineering Docs; Prompting Guide ReAct
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: observe before acting, state constraints, separate facts from hypotheses.
- Hermes-Adaptation:

```text
/goal Analyze read-only why <symptom> happens in <repo/system>. Gather evidence from logs, code, config, and recent error output. Separate facts, hypotheses, and unknowns. Do not write files, restart services, run traffic smokes, change config, or print secrets. Deliver: problem statement, evidence, most likely root cause, alternatives considered, risk, next safe action, and one concrete approval question if verification needs mutation. Done only when every claim is tied to evidence or labelled as a hypothesis.
```

- Eval-Level: L1 now; target L2 with synthetic logs.
- Known risks: logs may include secrets; summarize safely.

## Prompt Card: Research Synthesis With Source Quality

- Kategorie: goal | research
- Quelle(n): OpenAI Prompt Engineering Guide; Anthropic Prompt Engineering Docs
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: reference-grounded answer, criteria comparison, explicit uncertainty.
- Hermes-Adaptation:

```text
/goal Research <topic> for the decision <decision>. Use verifiable sources, evaluate source quality, compare at least three options against explicit criteria, and record uncertainty. Do not present unsupported claims as facts. Deliver: recommendation, reasoning, comparison table, source list with retrieval date, counterarguments, and residual risks. Done only when the recommendation can be traced to cited sources.
```

- Eval-Level: L1 now; target L2 with a fixed source set.
- Known risks: web sources drift; record retrieval date and access limits.

## Prompt Card: Docs Update With Implementation Check

- Kategorie: goal | docs
- Quelle(n): GitHub Copilot Prompt Engineering; Cursor Rules Docs
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: project rules + current implementation + executable examples.
- Hermes-Adaptation:

```text
/goal Update the documentation for <feature>. First inspect the current implementation, existing docs, and relevant commands. Change only the affected documentation files, keep examples executable, and verify with the smallest relevant docs, markdown, or lint check. Do not invent behavior not present in code. Stop with changed files, verification output, and residual risks.
```

- Eval-Level: L1 now.
- Known risks: docs can drift if implementation changes immediately after update.

## Prompt Card: Plan-Spec Only

- Kategorie: plan-spec | planning
- Quelle(n): Anthropic decomposition pattern; OpenAI task-splitting guidance
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: split raw idea into problem, target state, non-goals, artifacts, checks, and risks before implementation.
- Hermes-Adaptation:

```text
/goal Create only a plan-spec draft for <proposal>, without code or config changes. Check existing Hermes capabilities, define problem, target state, non-goals, MVP artifacts, acceptance criteria, eval/test strategy, risks, and follow-up cards. Stop when the self-contained Markdown draft is complete.
```

- Eval-Level: L1 now; target L2 with a reviewed example spec.
- Known risks: may become too theoretical unless follow-up cards are concrete.

## Prompt Card: Safe Ops Read-only Assessment

- Kategorie: goal | ops-readonly
- Quelle(n): Anthropic explicit constraints; ReAct observe/action separation
- Stand-Datum: 2026-06-11
- Abgeleitetes Muster: read-only observations, explicit forbidden actions, blocker on mutation.
- Hermes-Adaptation:

```text
/goal Check read-only the state of <system/feature>. Allowed actions are only file, config, log, and status reads. Do not write files, restart services, run production traffic smokes, change config, create tasks, or print secrets/PII. Deliver facts, evidence, hypotheses, risk, and next safe action. If mutation is needed to verify, stop with a concrete approval question. Done only when all observed facts cite their evidence.
```

- Eval-Level: L1 now; target L2 with synthetic config/log fixtures.
- Known risks: operators may expect active fixes; the prompt must preserve the read-only boundary.
