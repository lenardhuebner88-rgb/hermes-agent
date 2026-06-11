---
sidebar_position: 17
title: "Workflow Library"
description: "Prompt generators, curated prompt patterns, and evals for Hermes /goal workflows."
---

# Workflow Library

The Workflow Library is a bundled skill for building high-quality Hermes workflow prompts. It provides three prompt generators, a curated list of source-backed prompt patterns, and eval definitions for deciding when a prompt is good enough to reuse.

Use it when you want to turn a raw request into a safe, verifiable Hermes task.

## Relationship to Persistent Goals

Hermes currently ships the persistent loop command as [`/goal`](/user-guide/features/goals), also documented as Persistent Goals. `/goal` keeps an objective active across turns and uses a judge to decide `done` or `continue` after each turn.

`/loop` is not a separate Hermes slash command in this MVP (kein eigener Slash Command). In this page and in the `workflow-library` skill, "loop" means a prompt pattern for iterative `/goal` work: one small step, one check, then continue/done/block.

## What the library includes

Bundled skill path:

```text
skills/software-development/workflow-library/
```

Key files:

- `SKILL.md` - when to use the library, decision tree, pitfalls, and verification.
- `templates/goal-prompt-generator.md` - build a robust `/goal` prompt.
- `templates/loop-prompt-generator.md` - build an iterative loop-style `/goal` prompt.
- `templates/plan-spec-generator.md` - turn a raw idea into a plan-spec without implementation.
- `references/curated-online-prompts.md` - source-backed Prompt Cards adapted for Hermes.
- `references/eval-definition.md` - Eval Card, Score Rubric, Eval-Level, and metrics.
- `references/harness.md` - admission and test criteria for prompt templates.

## Goal-Prompt-Generator

Use this when the user has one bounded deliverable.

```text
/goal <task>. First read the relevant context, then work in the smallest safe steps, then verify with <command/source check>. Allowed: <allowed actions>. Forbidden: <forbidden actions>. Done only when <done criteria>; otherwise stop with one concrete blocker question and residual risk.
```

Minimum fields:

- task type,
- target path or source context,
- allowed actions,
- forbidden actions,
- narrow verification,
- Done criteria,
- blocker shape.

## Loop-Prompt-Generator

Use this when the user asks for iterative progress, for example codebase modernization or repeated cleanup. The generated command is still `/goal`.

```text
/goal Work iteratively toward <goal>. In each iteration: (1) choose the next smallest useful step, (2) inspect only the context needed for that step, (3) execute that one step, (4) run the narrowest verification, and (5) decide done, continue, or block. Track progress with <metric>. Do not exceed <scope boundaries>. Stop only when <done criteria> or a clear blocker is reached.
```

Good loop prompts name:

- one iteration step,
- a progress metric,
- stop/abort rules,
- forbidden actions,
- verification after each step.

## Plan-Spec-Generator

Use this when the task should produce a spec before any implementation.

```text
/goal Create only a plan-spec draft for <idea>, without changing code or config. Check existing Hermes capabilities, then write problem, target state, non-goals, MVP artifacts, acceptance criteria, eval/test strategy, risks, and follow-up cards. Stop only when the plan-spec is a self-contained Markdown artifact with residual risk.
```

A good plan-spec covers:

- problem,
- live capability check,
- target state,
- non-goals,
- MVP artifacts,
- acceptance criteria,
- eval/test strategy,
- follow-up cards,
- risks.

## Curated example prompts

The bundled skill includes source-backed Prompt Cards for:

1. Codebase Analysis Goal.
2. Codebase Modernization Loop.
3. Bugfix With Regression Test.
4. Read-only Incident Debugging.
5. Research Synthesis With Source Quality.
6. Docs Update With Implementation Check.
7. Plan-Spec Only.
8. Safe Ops Read-only Assessment.

The source list includes public prompt-engineering and agent-evaluation references such as Anthropic Prompt Engineering Docs, OpenAI Prompt Engineering Guide, GitHub Copilot Prompt Engineering, Cursor Rules Docs, Aider Usage Tips, SWE-bench, Terminal-Bench, SWE-agent, OpenHands, and HumanEval.

The library adapts patterns rather than copying external prompt text verbatim. Each Prompt Card records source, Stand-Datum, Hermes adaptation, Eval-Level, and known risks.

## Eval-Level

Prompt quality is tracked with these levels:

- L0 Static Lint: task, context, forbidden actions, Done criteria, and verification are present.
- L1 Human Review: maintainer checks clarity, safety, Hermes compatibility, and source adaptation.
- L2 Manual Sandbox Run: prompt is run against a temporary fixture or read-only snapshot.
- L3 Regression Harness: automated checks validate structure, `/goal` syntax, safety language, and forbidden patterns.
- L4 Agentic Benchmark Mapping: prompt is mapped to benchmark-like tasks such as SWE-bench-style fixes or Terminal-Bench-style terminal tasks.

MVP rule: every published Prompt Card needs at least L1. Core prompts should reach L2 before being called tested.

## Safety rules

- Use `/goal` as the executable command.
- Mention `/loop` only as a category or possible future alias, not as a current command.
- Forbid secrets/PII output.
- Forbid production writes, restarts, and traffic smokes unless explicitly approved.
- Avoid broad refactors and major upgrades without separate approval.
- Require real verification output or a clear blocker.
- Name residual risk in the final answer.

## Example: codebase analysis

```text
/goal Analyze <repo-path> read-only. Inspect README, project structure, dependency/config files, central modules, and representative tests. Do not write files, restart services, run production smoke traffic, or print secrets. Deliver: executive summary, file/command-backed facts, architecture map, top 5 risks, top 5 quick wins, larger modernization initiatives, recommended order, open questions, and residual risk for areas not inspected. Done only after the evidence sources are named.
```

## Example: codebase modernization

```text
/goal Modernize <repo-path> iteratively and safely. Start with a read-only plan covering runtime versions, dependencies, lint/test setup, deprecations, and security warnings. If changes are in scope, perform only low-risk, narrowly verifiable updates. One iteration equals one change class, such as one dependency group, one lint-fix type, or one small API migration. After each change, run the narrowest relevant check. Do not perform major upgrades, broad refactors, DB migrations, service restarts, or production changes without approval. Stop with plan, executed changes, checks, and residual risks.
```
