# Hermes Kanban Ingest / Worker / System Runbook

Audience: operators and agents that need to turn a goal into a safe Kanban task tree, keep workers running, and verify the system from live evidence.

This runbook is command-first and names the live code surfaces that implement each step so agents can verify behavior before acting.

## Live code anchors

| Layer | Code / command surface | What it owns |
| --- | --- | --- |
| Task ingest | `hermes_cli/kanban.py` -> `create`, `specify`, `decompose`; `plugins/kanban/dashboard/plugin_api.py` -> `/api/plugins/kanban/tasks` | Capturing root tasks, triage, body, assignee, dependencies, idempotency, notification subscription. |
| Task DB contract | `hermes_cli/kanban_db.py` | Status machine, dependencies, events, run rows, scope contract persistence, retries, diagnostics. |
| Decomposition | `hermes_cli/kanban_decompose.py`; PlanSpec hints in `hermes_cli/planspecs.py` | Root -> child graph, assignee routing, dependency shape, audit comments. |
| Worker runtime | `hermes_cli/kanban_worker_runtime.py` | Worker context, `HERMES_KANBAN_TASK`, heartbeat, completion/blocking, continuation behavior. |
| Dispatch loop | `hermes_cli/kanban.py dispatch`; gateway config `kanban.dispatch_in_gateway` | Reclaim stale claims, promote ready tasks, spawn workers within profile caps. Gateway owns the loop in production. |
| Observability | `hermes kanban show/tail/runs/log/diagnostics/stats`; dashboard Kanban APIs | Current task state, worker logs, run outcomes, diagnostics and rollups. |
| Safety | `tasks.scope_contract`; `guard-dangerous-ops.sh`; worker profile config | Tool/file scope narrowing and dangerous-operation blocking. Prose alone is not security-authoritative. |

## Golden path: ingest a goal

Use this path for operator-originated goals. Prefer a single triage root unless the graph is already obvious.

```bash
hermes kanban create   "<short root title>"   --triage   --assignee default   --kind ops   --body "<goal, constraints, done condition, gates>"   --idempotency-key "<stable-key>"   --json
```

Done when:

1. The command returns a task id.
2. `hermes kanban show <task_id>` shows the expected state.
3. The body contains goal, non-goals, acceptance checks, rollback/safety notes, and required worker lanes.

### Make the task reachable

- CLI-created tasks are subscribed to home notifications by default unless `--no-notify-home` is passed.
- `hermes kanban show <task_id>` gives local operator context.
- `hermes kanban context <task_id>` shows worker-visible context.
- Dashboard Kanban gives non-terminal visibility.
- Durable workstreams should use an epic:

```bash
hermes kanban epic create "<epic name>"
hermes kanban create "<root>" --triage --epic <epic_id> --body "..."
```

## Decompose: optimal worker placement

Production gateway has `auto_decompose` enabled; manual decomposition is a recovery/debug tool.

```bash
hermes kanban decompose <root_task_id> --author default --json
```

Routing:

| Work | Assignee |
| --- | --- |
| Multi-file code, tests, refactors | `coder` |
| Hard/large/reasoning-heavy code | `premium` |
| Reading, summary, evidence, web/doc reconnaissance | `research` |
| Pass/fail verdict gate only | `reviewer` |
| Adversarial critique / gap hunt | `critic` |

Graph rules:

- Independent lanes get no parents so the dispatcher can run them in parallel.
- Add parents only for a real dependency or write conflict.
- Never create a blind linear chain unless every card truly depends on the previous output.
- Every child needs a concrete done-condition: paths, commands, evidence expected, and out-of-scope items.

Verify the graph:

```bash
hermes kanban show <root_task_id>
hermes kanban list --assignee coder
hermes kanban list --assignee research
hermes kanban diagnostics --task <root_task_id> --json
```

Done when every child has a valid assignee, no invented assignee names exist, and diagnostics are empty or understood.

## Dispatch and worker operations

In production do not run a daemon or manual loop. The gateway dispatches every tick when `kanban.dispatch_in_gateway: true`.

Use manual dispatch only for local tests or scoped recovery:

```bash
hermes kanban dispatch --once
```

Worker lifecycle evidence:

```bash
hermes kanban stats
hermes kanban runs <task_id>
hermes kanban log <task_id>
hermes kanban tail <task_id>
```

Healthy worker signals:

- Task moves `ready -> running -> done` or `blocked` with a useful reason.
- `task_runs` has a row with profile, outcome, timestamps and summary.
- Running tasks emit heartbeat events before max-runtime.
- Completion names real task ids only; hallucinated ids become diagnostics.
- Review cards use verdict-only semantics.

Recovery commands:

```bash
# stale/wrong claim; inspect runs/logs first
hermes kanban reclaim <task_id>

# blocked but now fixed/allowed
hermes kanban unblock <task_id>

# wrong lane
hermes kanban reassign <task_id> <profile> --reclaim

# task spec is wrong and not running
hermes kanban respec <task_id> --body "<corrected spec>"
```

Escalate when recovery would require secrets, destructive DB/file changes, force-push/reset, upstream push, or outward production actions outside standing authorization.

## Scope contract and safety checklist

Before dispatching code-writing work, check:

1. `workspace` is explicit when code should be isolated (`worktree` or `worktree:<path>`).
2. `kind` reflects task class (`code`, `research`, `review`, `ops`, `analysis`, `text`).
3. Security-relevant restrictions are in `scope_contract`, not only prose.
4. `allowed_tools`, writable paths, and anti-scope are narrow enough.
5. Dangerous operations are excluded or routed to a human-decision card.

Inspect persisted scope:

```bash
python - <<'PYCODE'
import sqlite3, json, os
conn = sqlite3.connect(os.path.expanduser('~/.hermes/kanban.db'))
conn.row_factory = sqlite3.Row
for row in conn.execute("select id,title,assignee,kind,scope_contract from tasks where id=?", ('<task_id>',)):
    print(dict(row))
    if row['scope_contract']:
        print(json.dumps(json.loads(row['scope_contract']), indent=2))
PYCODE
```

## System readiness checklist

Run this before blaming a worker model:

```bash
hermes kanban assignees
hermes kanban diagnostics --json
hermes kanban stats
hermes kanban list --status running
hermes kanban list --status blocked
hermes kanban list --status ready
```

Expected:

- Required profiles exist on disk: `coder`, `premium`, `research`, `reviewer`, `critic`.
- No ready backlog is stuck behind a non-existent assignee.
- Diagnostics do not show critical stale claims, impossible deps, missing workspace, or repeated spawn failures.
- Gateway owns dispatch; no detached dispatch loops or orphan workers run.

## End-to-end smoke test without touching production board

Use a temporary Hermes home and `HERMES_SANDBOX_MODE=1` so the test exercises real DB/CLI code but cannot mutate `~/.hermes/kanban.db`, even when a worker inherited live-board env vars. Add minimal dummy profile configs so spawnable-assignee validation exercises the real worker roster names without using credentials. Real model/profile readiness is still validated separately with `hermes kanban assignees` against the operator home.

```bash
export HERMES_HOME="$(mktemp -d)"
export HERMES_SANDBOX_MODE=1
for p in coder premium research reviewer critic default; do
  mkdir -p "$HERMES_HOME/profiles/$p"
  printf "model:\n  provider: test\n  name: test\n" > "$HERMES_HOME/profiles/$p/config.yaml"
done
hermes kanban init
root=$(hermes kanban create "smoke root" --triage --assignee default --kind ops --body "Smoke root only." --json | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
hermes kanban show "$root"
hermes kanban create "smoke research" --assignee research --parent "$root" --kind research --body "Return evidence only." --json
hermes kanban create "smoke review" --assignee reviewer --parent "$root" --kind review --body "Verdict only." --json
hermes kanban diagnostics --json
hermes kanban stats
```

Pass criteria:

- `init`, both `create` commands, `show`, `diagnostics`, and `stats` exit 0.
- The root and children appear in the temp DB.
- Diagnostics are empty or explainable for the artificial graph.
- Live-board env vars such as `HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD` do not redirect writes back to production.
- Removing `$HERMES_HOME` deletes the smoke board.

## Mother receipt template

```text
Ergebnis: <what was achieved>
Geändert: <tasks/docs/code/config touched>
Gates: <commands and exact results>
Offen: <residual risks / next decision>
```

Record receipts under `vault/03-Agents/Hermes/receipts/` for operator work, then close/complete the Kanban root when all children and gates are settled.
