---
title: "Kanban Worker — Pitfalls, examples, and edge cases for Hermes Kanban workers"
sidebar_label: "Kanban Worker"
description: "Pitfalls, examples, and edge cases for Hermes Kanban workers"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Kanban Worker

Pitfalls, examples, and edge cases for Hermes Kanban workers. The lifecycle itself is auto-injected into every worker's system prompt as KANBAN_GUIDANCE (from agent/prompt_builder.py); this skill is what you load when you want deeper detail on specific scenarios.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/devops/kanban-worker` |
| Version | `2.0.0` |
| Platforms | linux, macos, windows |
| Tags | `kanban`, `multi-agent`, `collaboration`, `workflow`, `pitfalls` |
| Related skills | [`kanban-orchestrator`](/docs/user-guide/skills/bundled/devops/devops-kanban-orchestrator) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Kanban Worker — Pitfalls and Examples

> You're seeing this skill because the Hermes Kanban dispatcher spawned you as a worker with `--skills kanban-worker` — it's loaded automatically for every dispatched worker. The **lifecycle** (6 steps: orient → work → heartbeat → block/complete) also lives in the `KANBAN_GUIDANCE` block that's auto-injected into your system prompt. This skill is the deeper detail: good handoff shapes, retry diagnostics, edge cases.

## Workspace handling

Your workspace kind determines how you should behave inside `$HERMES_KANBAN_WORKSPACE`:

| Kind | What it is | How to work |
|---|---|---|
| `scratch` | Fresh tmp dir, yours alone | Read/write freely. **It is DELETED the moment you `kanban_complete`** (not when archived) — to keep any deliverable, pass its absolute path in `kanban_complete(artifacts=[...])` (see "Preserving deliverables" below). |
| `dir:<path>` | Shared persistent directory | Other runs will read what you write. Treat it like long-lived state. Path is guaranteed absolute (the kernel rejects relative paths). |
| `worktree` | Git worktree at the resolved path | If `.git` doesn't exist, run `git worktree add <path> ${HERMES_KANBAN_BRANCH:-wt/$HERMES_KANBAN_TASK}` from the main repo first, then cd and work normally. Commit work here. |

## Tenant isolation

If `$HERMES_TENANT` is set, the task belongs to a tenant namespace. When reading or writing persistent memory, prefix memory entries with the tenant so context doesn't leak across tenants:

- Good: `business-a: Acme is our biggest customer`
- Bad (leaks): `Acme is our biggest customer`

## Preserving deliverables

A `scratch` workspace is wiped as soon as you `kanban_complete` — only the run's `summary`/`metadata` strings survive in the DB. If your task produced a real file the operator should be able to open later (a `RESULT.md`, a generated report, a build artifact), pass its **absolute** path in the `artifacts` list:

```python
kanban_complete(
    summary="generated the weekly digest",
    artifacts=["/abs/path/in/workspace/digest.md"],  # copied out BEFORE the wipe
)
```

Each listed artifact that resolves *inside* your scratch workspace is copied to `~/.hermes/reports/by-task/<your-task-id>/<basename>` before deletion, and a `deliverables_preserved` event records where they landed. Pass absolute paths (workspace-relative paths are skipped). Anything you don't list is gone.

## Don't end your turn without completing

The dispatcher treats a clean exit while the task is still `running` as a **protocol violation**: it counts a failed run and auto-blocks the task. Your run is only over once you call `kanban_complete` or `kanban_block`. Never answer in prose and stop.

## Scope contract is advisory

If the task body carries a `scope_contract.allowed_tools` list, treat it as an attestation of intent — **not** a runtime allowlist. Your actual tools come from your profile. If you need a tool your profile grants (e.g. `write_file`, `terminal`) that the contract didn't enumerate, use it; do not block solely because it's absent from the list.

## Good summary + metadata shapes

The `kanban_complete(summary=..., metadata=...)` handoff is how downstream workers read what you did. Patterns that work:

**Coding task:**
```python
kanban_complete(
    summary="shipped rate limiter — token bucket, keys on user_id with IP fallback, 14 tests pass",
    metadata={
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "decisions": ["user_id primary, IP fallback for unauthenticated requests"],
    },
)
```

**Coding task that needs review (the verifier gate):**

Code changes DO get reviewed — but you request that review by **completing**, not blocking. When your code task is done and your gates pass, call `kanban_complete` with the structured handoff. If you're a code role (e.g. `coder`, `premium`), the **review gate** automatically parks your completion in `review` and hands it to an independent `verifier` profile, which re-runs the real gates (tests/build/lint) on your actual changes and renders the verdict: APPROVED → the task moves to `done`; REQUEST_CHANGES → it's blocked for a follow-up fix with the failing command output attached. **That verifier IS the review** — so completing is how you ask for it.

Put the handoff in `kanban_complete` itself — its `summary` / `metadata` / `artifacts` carry everything the verifier and the dashboard read, so you don't need a separate comment + block:

```python
kanban_complete(
    summary="shipped rate limiter — token bucket, keys on user_id with IP fallback, 14/14 tests pass",
    metadata={
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "decisions": ["user_id primary, IP fallback for unauthenticated requests"],
    },
    artifacts=["/abs/path/to/RESULT.md"],  # optional; preserved past the scratch wipe
)
```

Do **not** end a finished, gates-green code task with `kanban_block(reason="review-required: ...")`. `blocked` is a sticky, human-gated dead end that no reviewer loop consumes — it bypasses the verifier and stalls every task that depends on yours. Reserve `kanban_block` for a genuine blocker: a decision only a human can make, a missing credential, a contradiction in the task (see "Block reasons that get answered fast" below).

**Research task:**
```python
kanban_complete(
    summary="3 competing libraries reviewed; vLLM wins on throughput, SGLang on latency, Tensorrt-LLM on memory efficiency",
    metadata={
        "sources_read": 12,
        "recommendation": "vLLM",
        "benchmarks": {"vllm": 1.0, "sglang": 0.87, "trtllm": 0.72},
    },
)
```

**Review task:**
```python
kanban_complete(
    summary="reviewed PR #123; 2 blocking issues found (SQL injection in /search, missing CSRF on /settings)",
    metadata={
        "pr_number": 123,
        "findings": [
            {"severity": "critical", "file": "api/search.py", "line": 42, "issue": "raw SQL concat"},
            {"severity": "high", "file": "api/settings.py", "issue": "missing CSRF middleware"},
        ],
        "approved": False,
    },
)
```

Shape `metadata` so downstream parsers (reviewers, aggregators, schedulers) can use it without re-reading your prose.

## Claiming cards you actually created

If your run produced new kanban tasks (via `kanban_create`), pass the ids in `created_cards` on `kanban_complete`. The kernel verifies each id exists and was created by your profile; any phantom id blocks the completion with an error listing what went wrong, and the rejected attempt is permanently recorded on the task's event log. **Only list ids you captured from a successful `kanban_create` return value — never invent ids from prose, never paste ids from earlier runs, never claim cards another worker created.**

```python
# GOOD — capture return values, then claim them.
c1 = kanban_create(title="remediate SQL injection", assignee="security-worker")
c2 = kanban_create(title="fix CSRF middleware", assignee="web-worker")

kanban_complete(
    summary="Review done; spawned remediations for both findings.",
    metadata={"pr_number": 123, "approved": False},
    created_cards=[c1["task_id"], c2["task_id"]],
)
```

```python
# BAD — claiming ids you don't have captured return values for.
kanban_complete(
    summary="Created remediation cards t_a1b2c3d4, t_deadbeef",  # hallucinated
    created_cards=["t_a1b2c3d4", "t_deadbeef"],                   # → gate rejects
)
```

If a `kanban_create` call fails (exception, tool_error), the card was NOT created — do not include a phantom id for it. Retry the create, or omit the id and mention the failure in your summary. The prose-scan pass also catches `t_<hex>` references in your free-form summary that don't resolve; these don't block the completion but show up as advisory warnings on the task in the dashboard.

## Block reasons that get answered fast

Bad: `"stuck"` — the human has no context.

Good: one sentence naming the specific decision you need. Leave longer context as a comment instead.

```python
kanban_comment(
    task_id=os.environ["HERMES_KANBAN_TASK"],
    body="Full context: I have user IPs from Cloudflare headers but some users are behind NATs with thousands of peers. Keying on IP alone causes false positives.",
)
kanban_block(reason="Rate limit key choice: IP (simple, NAT-unsafe) or user_id (requires auth, skips anonymous endpoints)?")
```

The block message is what appears in the dashboard / gateway notifier. The comment is the deeper context a human reads when they open the task.

## Heartbeats worth sending

Good heartbeats name progress: `"epoch 12/50, loss 0.31"`, `"scanned 1.2M/2.4M rows"`, `"uploaded 47/120 videos"`.

Bad heartbeats: `"still working"`, empty notes, sub-second intervals. Every few minutes max; skip entirely for tasks under ~2 minutes.

## Retry scenarios

If you open the task and `kanban_show` returns `runs: [...]` with one or more closed runs, you're a retry. The prior runs' `outcome` / `summary` / `error` tell you what didn't work. Don't repeat that path. Typical retry diagnostics:

- `outcome: "timed_out"` — the previous attempt hit `max_runtime_seconds`. You may need to chunk the work or shorten it.
- `outcome: "crashed"` — OOM or segfault. Reduce memory footprint.
- `outcome: "spawn_failed"` + `error: "..."` — usually a profile config issue (missing credential, bad PATH). Ask the human via `kanban_block` instead of retrying blindly.
- `outcome: "reclaimed"` + `summary: "task archived..."` — operator archived the task out from under the previous run; you probably shouldn't be running at all, check status carefully.
- `outcome: "blocked"` — a previous attempt blocked; the unblock comment should be in the thread by now.

## Notification routing

You can configure the gateway to receive cross-profile Kanban task notifications by adding `notification_sources` to `~/.hermes/config.yaml`.
- `notification_sources: ['*']` accepts subscriptions from all profiles.
- `notification_sources: ['default', 'zilor-ppt']` or `"default,zilor-ppt"` restricts subscriptions to specified profiles.
- Omitting the key keeps the default behavior (profile isolation).

## Do NOT

- Call `delegate_task` as a substitute for `kanban_create`. `delegate_task` is for short reasoning subtasks inside YOUR run; `kanban_create` is for cross-agent handoffs that outlive one API loop.
- Call `clarify` to ask the human a question. You are running headless — there is no live user to answer. The call will time out (default ~120s) and the task will sit silently in `running` with no signal that it needs input. Use `kanban_comment` (context) + `kanban_block(reason=...)` (decision needed) instead — the task surfaces on the board as blocked, the operator sees it, unblocks with their answer in a comment, and you respawn with the thread.
- Modify files outside `$HERMES_KANBAN_WORKSPACE` unless the task body says to.
- Create follow-up tasks assigned to yourself — assign to the right specialist.
- Complete a task you didn't actually finish. Block it instead.

## Pitfalls

**Task state can change between dispatch and your startup.** Between when the dispatcher claimed and when your process actually booted, the task may have been blocked, reassigned, or archived. Always `kanban_show` first. If it reports `blocked` or `archived`, stop — you shouldn't be running.

**Workspace may have stale artifacts.** Especially `dir:` and `worktree` workspaces can have files from previous runs. Read the comment thread — it usually explains why you're running again and what state the workspace is in.

**Don't rely on the CLI when the guidance is available.** The `kanban_*` tools work across all terminal backends (Docker, Modal, SSH). `hermes kanban <verb>` from your terminal tool will fail in containerized backends because the CLI isn't installed there. When in doubt, use the tool.

## CLI fallback (for scripting)

Every tool has a CLI equivalent for human operators and scripts:
- `kanban_show` ↔ `hermes kanban show <id> --json`
- `kanban_complete` ↔ `hermes kanban complete <id> --summary "..." --metadata '{...}'`
- `kanban_block` ↔ `hermes kanban block <id> "reason"`
- `kanban_create` ↔ `hermes kanban create "title" --assignee <profile> [--parent <id>]`
- etc.

Use the tools from inside an agent; the CLI exists for the human at the terminal.

## Sandboxing kanban side-effects in worker-spawned scripts (HERMES_SANDBOX_MODE)

**Warning:** A worker process inherits `HERMES_KANBAN_DB` and
`HERMES_KANBAN_BOARD` from the dispatcher, both pinned to the LIVE
production board. Any helper script you launch (sample-builder, fixture
generator, test harness, etc.) that calls
`hermes kanban create` / `kanban_db.create_task()` will land rows on the
LIVE board unless you explicitly opt into sandbox mode.

This is the **live-DB-leak footgun**: a coder run on 2026-05-27 created
3 accidental tasks (`t_5d4138fe`, `t_632b8e1b`, `t_63f39a2f`) on the
production board from a sample-builder script. The self-recovery worked
but the pattern is dangerous in autonomy.

### The fix: opt into HERMES_SANDBOX_MODE=1

In any shell script that calls `hermes kanban <write-verb>`:

```bash
#!/usr/bin/env bash
export HERMES_SANDBOX_MODE=1
# Now `hermes kanban create / promote / complete / link / ...` writes
# go to `${HERMES_HOME}/.kanban-sandbox/default.db` instead of the
# live `${HERMES_HOME}/kanban.db`.
hermes kanban create "smoke-test task" --body "sandbox"
hermes kanban list   # shows the sandbox board
```

Equivalent in a Python sub-process:

```python
import os, subprocess
env = {**os.environ, "HERMES_SANDBOX_MODE": "1"}
subprocess.run(
    ["hermes", "kanban", "create", "smoke", "--body", "sandbox"],
    env=env, check=True,
)
```

`HERMES_SANDBOX_MODE=1`:
- redirects `kanban_db_path()` → `${HERMES_HOME}/.kanban-sandbox/<board>.db`
- redirects `workspaces_root()` → `${HERMES_HOME}/.kanban-sandbox/workspaces/`
- **takes precedence over `HERMES_KANBAN_DB`** (so it survives the
  dispatcher's worker-env injection)
- ignores `HERMES_KANBAN_BOARD` (uses sandbox-local "default" board)

When you're done, wipe the sandbox to start fresh:

```bash
rm -rf "${HERMES_HOME}/.kanban-sandbox"
```

**Inverse rule:** if you genuinely need a worker script to mutate the
LIVE board (rare, usually a coordinator-style helper), call the tool
directly via `kanban_create(...)` — that is the documented audited
path. Don't shell out from inside a worker to mutate the live board.
