---
name: workflow-library
description: "Workflow library for Hermes /goal prompts, loop-style work, plan-spec drafting, curated prompt patterns, and eval harnesses."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [workflow, prompts, goal, loop, evals, planning, software-development]
---

# Hermes Workflow Library

Use this skill when a user wants a tested Hermes workflow prompt, a prompt generator, a `/goal` loop-style task, or a plan-spec prompt before implementation.

This library is intentionally a skill/docs slice. It does not add a new slash command, mutate runtime config, restart the gateway, or change the `/goal` judge. In the MVP, `/loop` is a search term and prompt category for iterative work; Hermes currently uses `/goal` as the actual command.

## Goal vs Loop vs Plan-Spec

- Choose **Goal** when the user has one bounded outcome and wants Hermes to keep working until done or blocked.
- Choose **Loop** when the user asks for iterative progress over small steps. Generate a `/goal` prompt that names the per-iteration step, progress metric, and stop/abort rules.
- Choose **Plan-Spec** when the user has a raw idea and needs a reviewed implementation spec before code/config changes.

## Quickstart

1. Pick the closest generator:
   - `templates/goal-prompt-generator.md`
   - `templates/loop-prompt-generator.md`
   - `templates/plan-spec-generator.md`
2. Fill in task type, context, allowed actions, forbidden actions, done criteria, and verification.
3. If the task matches a known pattern, start from a ready template:
   - `templates/codebase-analysis-goal.md`
   - `templates/codebase-modernization-goal.md`
   - `templates/debugging-goal.md`
   - `templates/research-goal.md`
   - `templates/docs-goal.md`
   - `templates/ops-readonly-goal.md`
4. For library additions, create both a Prompt Card and Eval Card using the references.

## Curated prompt sources

The curated source list lives in `references/curated-online-prompts.md`. Do not copy external prompts verbatim by default. Extract the durable pattern, cite the source and Stand-Datum, adapt it to Hermes, and record an Eval-Level.

## Safety rules

- Always state the actual command as `/goal`, unless explicitly documenting the `/loop` terminology.
- Do not imply `/loop` is a shipped Hermes slash command.
- Include explicit forbidden actions: no secrets/PII output, no production mutations, no restarts, no broad refactors, no YOLO/approval bypass unless the operator explicitly grants it.
- Prefer narrow verification commands and real tool/source evidence.
- If verification needs mutation or credentials outside scope, stop with a concrete blocker question.

## Pitfalls

- Vague goals make the judge stop too early or loop too long.
- Missing verification lets polished summaries pass without evidence.
- Broad modernization prompts can burn budget; require one change class per iteration.
- Read-only analysis prompts must forbid writes, restarts, smoke traffic, and config edits.
- Online prompt lists drift; keep source URLs and Stand-Datum with every Prompt Card.

## Verification

Before using or adding a workflow prompt, check:

- Clear task and target context.
- Allowed and forbidden actions are explicit.
- Done or Stop criteria include a concrete deliverable.
- Verification requires real output or cites a blocker.
- Rest risk must be named.
- Prompt Card has at least L1 Human Review; core prompts should reach L2 Manual Sandbox Run before being described as tested.

See:

- `references/eval-definition.md`
- `references/harness.md`
- `references/examples.md`
- `references/anti-patterns.md`
