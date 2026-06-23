# Runbook: Per-Repo Concurrency Cap — Live Dogfood Evidence

## Overview

This runbook describes the reproducible operator procedure to validate the
per-repo concurrency cap (`max_concurrent_per_repo`) against the live Hermes
board with real workers. Evidence is captured by
`scripts/dogfood_repo_cap_evidence.py` (READ-ONLY collector).

## Prerequisites

- Hermes gateway running on `http://127.0.0.1:9119` (default).
- `HERMES_DASHBOARD_USERNAME` and `HERMES_DASHBOARD_PASSWORD` set in the environment.
- `max_concurrent_per_repo` configured in `config.yaml` (default: 1).
- Two disjunct code tasks ready on the board (for S1/S3), or two same-lane tasks (for S2).
- The repo at `/home/piet/.hermes/hermes-agent` with a clean `main` branch.

## Collector Usage

```bash
# Full evidence collection for one scenario
python3 scripts/dogfood_repo_cap_evidence.py \
    --task-ids t_xxx t_yyy \
    --scenario S1 \
    --repo /home/piet/.hermes/hermes-agent \
    --receipt-dir /home/piet/vault/03-Agents/Claude-Code/receipts

# Dry-run (validates args, writes template, no API calls)
python3 scripts/dogfood_repo_cap_evidence.py --dry-run

# Short poll for quick snapshot (no task activity)
python3 scripts/dogfood_repo_cap_evidence.py \
    --task-ids "" --scenario S1 \
    --poll-duration 10 --poll-interval 2

# Sandbox repo whose default branch is NOT 'main' (e.g. trunk/master)
python3 scripts/dogfood_repo_cap_evidence.py \
    --repo /tmp/sandbox-repo --branch trunk \
    --scenario S1 --receipt-dir /tmp/dogfood-receipts
```

### What the collector captures

| Data source | API endpoint | Evidence |
|-------------|-------------|----------|
| workers/active | `GET /api/plugins/kanban/workers/active` | max concurrent worker count, task_ids, assignees |
| task activity | `GET /api/plugins/kanban/tasks/{id}/activity` | `integration_merged`, `integration_rebase_conflict`, `repo_serialized` events |
| git log | `git -C <repo> log --oneline <branch>` | landed commits on the chosen branch |

The collector NEVER prints or writes passwords, tokens, or cookies.
Receipts are written to the specified `--receipt-dir`.

### Branch handling (non-`main` sandboxes)

The git-log source defaults to `--branch main` (the Hermes repo's default branch).
For a `--repo` sandbox whose default branch is **not** `main` (e.g. `trunk` or
`master`), either:

- pass `--branch <name>` explicitly to read that branch, **or**
- rely on the automatic fallback: if the requested branch does not resolve, the
  collector reads `HEAD` instead, so evidence is still captured rather than an
  empty `[git error]` line.

`--receipt-dir ''` (empty) writes the receipt/template to the current working
directory instead of crashing.

## Scenario 1: Echte Parallelität (Real Parallelism)

**Goal:** Prove that `max_concurrent_per_repo=2` allows two workers to run
simultaneously on the same repo without serialization.

**Setup:**

1. Set `max_concurrent_per_repo: 2` in `config.yaml`.
2. Restart gateway: `systemctl --user restart hermes-gateway.service`.
3. Create two DISJOINT code tasks on the Hermes repo (different files, no overlap).
4. Assign one to `coder` and one to `premium` (lane-spread to avoid per_profile=1 serialization).

**Execute:**

```bash
# While both tasks are running (or about to run):
python3 scripts/dogfood_repo_cap_evidence.py \
    --task-ids t_AAA t_BBB \
    --scenario S1 \
    --poll-duration 60 --poll-interval 3
```

**Expected evidence in receipt:**

- `workers/active` count == 2 in at least one snapshot.
- Both task_ids present in workers/active simultaneously.
- Both tasks have `integration_merged` event.
- `git log main` shows both commits.
- NO `repo_serialized` event between the two `integration_merged` events.

## Scenario 2: No-Op-Falle (No-Op Trap)

**Goal:** Confirm that `cap=2` does NOT help when both tasks are on the same
lane with `per_profile=1` — the activation caveat from the build PlanSpec.

**Setup:**

1. Keep `max_concurrent_per_repo: 2`.
2. Create two tasks assigned to the SAME lane (e.g., both `coder`).
3. Ensure `max_in_progress` per profile is 1 (default).

**Execute:**

```bash
python3 scripts/dogfood_repo_cap_evidence.py \
    --task-ids t_CCC t_DDD \
    --scenario S2 \
    --poll-duration 60 --poll-interval 3
```

**Expected evidence in receipt:**

- `workers/active` count never reaches 2 for these tasks (same profile serialized).
- `repo_serialized` or `skipped_repo_serialized` event present for at least one task.
- This proves the cap alone is insufficient without lane-spread.

## Scenario 3: Overlap-Konflikt (File Overlap Conflict)

**Goal:** Verify that when two tasks modify the SAME file, the second
integration hits a rebase conflict and is blocked.

**Setup:**

1. Keep `max_concurrent_per_repo: 2`.
2. Create two tasks that both modify the same file (e.g., both edit `README.md`).
3. Assign to different lanes (`coder` + `premium`).

**Execute:**

```bash
python3 scripts/dogfood_repo_cap_evidence.py \
    --task-ids t_EEE t_FFF \
    --scenario S3 \
    --poll-duration 60 --poll-interval 3
```

**Expected evidence in receipt:**

- Second task has `integration_rebase_conflict` event (not `integration_merged`).
- Second task ends up `blocked` (fixer route).
- `git log main` only shows the winner's commit.
- First task may have `integration_merged`.

## Scenario 4: Rollback (Restore Serialization)

**Goal:** Confirm that setting `cap=1` restores serialization behavior.

**Setup:**

1. Set `max_concurrent_per_repo: 1` in `config.yaml`.
2. Restart gateway: `systemctl --user restart hermes-gateway.service`.
3. Queue N+1 tasks (e.g., 3 tasks) on the same repo with lane-spread.

**Execute:**

```bash
python3 scripts/dogfood_repo_cap_evidence.py \
    --task-ids t_GGG t_HHH t_III \
    --scenario S4 \
    --poll-duration 90 --poll-interval 5
```

**Expected evidence in receipt:**

- `workers/active` count never exceeds 1 for the same repo.
- `repo_serialized` events present for the surplus tasks.
- Tasks serialize: only one `integration_merged` at a time.

## Receipt Storage

All receipts are written to:
`/home/piet/vault/03-Agents/Claude-Code/receipts/`

Naming convention:
`YYYY-MM-DD-dogfood-repo-cap-{scenario}-receipt.md`

The receipt contains:
- Timestamps (UTC start/end)
- Peak concurrent worker count
- Workers/active JSON snapshots (secrets scrubbed)
- Per-task filtered events (integration_merged, repo_rebase_conflict, repo_serialized)
- Git log of main
- Scenario expectations checklist

## Rollback / Cleanup

After dogfood is complete:

1. Restore `max_concurrent_per_repo` to the production value (typically 1).
2. Restart gateway: `systemctl --user restart hermes-gateway.service`.
3. Verify serialisation is restored (run S4 procedure).
4. Archive or clean up any test tasks created for dogfood.
