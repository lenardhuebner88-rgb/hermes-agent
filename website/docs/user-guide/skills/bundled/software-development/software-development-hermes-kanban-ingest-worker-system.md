---
title: hermes-kanban-ingest-worker-system
---

---
name: hermes-kanban-ingest-worker-system
description: Use when creating, decomposing, dispatching, debugging, or reviewing Hermes Kanban task ingest/worker/system flows. Provides the canonical runbook path, routing rules, safety checks, and smoke-test commands.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hermes, kanban, ingest, workers, dispatcher, operations, runbook]
    related_skills: [hermes-agent, kanban-review-operations, hermes-dispatcher-worker-runner, hermes-kanban-worker-scope-control]
---

# Hermes Kanban Ingest / Worker / System

Use this skill when you need to turn an operator goal into a safe Kanban task tree, verify worker placement, debug a stuck worker, or smoke-test the Kanban system.

The detailed shared runbook lives in:

- `docs/kanban/ingest-worker-system-runbook.md`

## Fast path

1. Inspect current config/board/code before concluding. Do not rely on stale memory for system state.
2. Ingest as a triage root unless the task graph is already obvious:
   ```bash
   hermes kanban create "<root>" --triage --assignee default --kind ops --body "<goal, gates, non-goals>" --idempotency-key "<stable-key>" --json
   ```
3. Route children by lane: `coder` for normal code, `premium` for hard code, `research` for read-only evidence, `reviewer` for verdict, `critic` for adversarial critique. Never invent assignee names.
4. Keep graph parallel: independent children have no parents; add parents only for real data/write dependencies.
5. Verify before dispatch/recovery:
   ```bash
   hermes kanban show <task_id>
   hermes kanban context <task_id>
   hermes kanban diagnostics --task <task_id> --json
   hermes kanban assignees
   ```
6. Let the gateway dispatch in production. Do not start detached dispatch loops. Manual `hermes kanban dispatch --once` is for local tests or scoped recovery only.
7. Observe with evidence: `stats`, `runs`, `log`, `tail`, `diagnostics` before changing state.
8. Recover narrowly: reclaim stale claims, unblock resolved cards, reassign wrong lanes, or respec non-running cards. Escalate for secrets, destructive changes, force/reset/push-upstream, or outward production mutations not covered by standing authorization.

## Smoke test pattern

Exercise real CLI/DB code without touching the production board:

```bash
export HERMES_HOME="$(mktemp -d)"
for p in coder premium research reviewer critic default; do
  mkdir -p "$HERMES_HOME/profiles/$p"
  printf "model:\n  provider: test\n  name: test\n" > "$HERMES_HOME/profiles/$p/config.yaml"
done
hermes kanban init
root=$(hermes kanban create "smoke root" --triage --assignee default --kind ops --body "Smoke root only." --json | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
hermes kanban show "$root"
hermes kanban create "smoke research" --assignee research --parent "$root" --kind research --body "Return evidence only." --json
hermes kanban diagnostics --json
hermes kanban stats
```

Pass when every command exits 0 and the temp board contains the expected root/child graph.

## Completion criteria

A Kanban ingest/worker/system task is not done until:

- the task graph is reachable from CLI/dashboard/home notifications,
- every child has a valid on-disk profile assignee,
- scope/workspace/tool constraints are persisted where security-relevant,
- live diagnostics/logs/runs were checked,
- tests or smoke commands were run against current code, and
- a receipt names changed paths, commands, exact results, open risk, and next decision.
