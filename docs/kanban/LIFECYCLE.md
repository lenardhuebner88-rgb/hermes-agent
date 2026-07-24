# Kanban task lifecycle map

`hermes_cli/kanban_db.py` is larger than 1 MiB, so CodeGraph does not index it.
Use `rg` plus this map, not CodeGraph results for similarly named test doubles.
Line anchors are verified by `scripts/check_kanban_lifecycle_anchors.py`; run it
after rebasing or changing the monolith.

Source of truth for the state vocabulary:
[`VALID_STATUSES`](../../hermes_cli/kanban_db.py#L126).

## State diagram

```text
                         create(triage)
                              │
                              ▼
                           triage ── specify ──► todo
                              ▲                  │  parents done / due / wait released
                 repeated     │                  ▼
                 block ───────┘                ready ◄──────────────┐
                                                   │ claim          │ reclaim / retry /
                                  schedule         ▼                │ workflow next step
                    ┌────────────────────────── running ─────────────┘
                    │                              │  │
                    ▼                              │  ├─ block ─────► blocked
                 scheduled                         │  │                  │
                    │                              │  │                  ├─ unblock ─► todo|ready
                    └──────── unblock ─────► todo|ready                 │
                                                   │  └─ submit ────► review
                                                   │                      │ claim review
                                                   │                      ▼
                                                   │                   running
                                                   │                      │
                                                   │       REQUEST_CHANGES├─► blocked
                                                   │              APPROVED├─► review (next stage)
                                                   │                      └─► done (final stage)
                                                   └──────── complete ───────► done

             triage | todo | scheduled | ready | running | blocked | review | done
                                      └──────── archive ────────────────► archived
```

`archived` is terminal for dispatch. `done` is dependency satisfaction; an
archived parent is deliberately not treated as done.

## Transition table

| from → to | function (anchored) | who triggers it | guard that can block it | observable symptom when it stalls |
|---|---|---|---|---|
| creation → triage | [`create_task`](../../hermes_cli/kanban_db.py#L4931) | CLI / automation | invalid initial fields, missing parents, role/workspace contract | no row is created; caller gets a validation error |
| creation → todo | [`create_task`](../../hermes_cli/kanban_db.py#L4931) | CLI / automation | missing parent IDs | new card waits in `todo`; an unfinished parent is visible |
| creation → ready | [`create_task`](../../hermes_cli/kanban_db.py#L4931) | CLI / automation | unfinished parents route to `todo` instead | card is `todo`, not `ready` |
| creation → blocked | [`create_task`](../../hermes_cli/kanban_db.py#L4931) | CLI / automation | only the explicit blocked initial mode is accepted | card is born as an operator park |
| triage → todo | [`specify_triage_task`](../../hermes_cli/kanban_db.py#L18319) | CLI / sweep | row must still be in `triage` | specify returns false; status did not change |
| todo → ready | [`recompute_ready`](../../hermes_cli/kanban_db.py#L11165) | dispatcher daemon / completion sweep | parents, due time, typed wait, sticky block, active escalation, failure limit | card remains `todo` or `blocked`; no `promoted` event |
| todo\|blocked → ready | [`promote_task`](../../hermes_cli/kanban_db.py#L17475) | CLI | typed wait or unfinished parents; blocked tasks cannot force past parents | command reports the refusing parent/wait |
| ready → todo | [`claim_task`](../../hermes_cli/kanban_db.py#L11305) | dispatcher daemon / CLI claimer | active wait or any parent not `done` | `claim_rejected` event; reason is `active_wait` or `parents_not_done` |
| ready → running | [`claim_task`](../../hermes_cli/kanban_db.py#L11305) | dispatcher daemon / CLI claimer | code contract, wait, parents, claim CAS | card stays `ready`; no current run is opened |
| review → running | [`claim_review_task`](../../hermes_cli/kanban_db.py#L11544) | review gate / dispatcher daemon | already claimed, wrong status, unavailable review profile before claim | card stays `review`; review hold event identifies the target |
| running\|ready → blocked | [`block_task`](../../hermes_cli/kanban_db.py#L17134) | worker / CLI / review gate | stale run ID, wrong status, invalid review-origin contract | block returns false; rejection event explains the mismatch |
| running\|ready → todo | [`block_task`](../../hermes_cli/kanban_db.py#L17134) | worker | a valid unsatisfied dependency wait deliberately parks on `todo` | `wait_registered` plus a `blocked` event whose status is `todo` |
| running\|ready → triage | [`block_task`](../../hermes_cli/kanban_db.py#L17134) | worker / sweep | same non-review block kind must reach [`BLOCK_RECURRENCE_LIMIT`](../../hermes_cli/kanban_db.py#L172) | `block_loop_detected` event; card is in `triage` |
| running\|ready\|blocked → review | [`_submit_for_review`](../../hermes_cli/kanban_db.py#L14679) | worker / review gate | workflow identity, worker gate, expected run CAS | task blocks on identity failure or completion raises a worker-gate error |
| running → blocked | [`hold_task`](../../hermes_cli/kanban_db.py#L12231) | CLI | task must still be running | hold returns false; worker/status won the race |
| triage\|todo\|scheduled\|ready\|running\|review → blocked | [`cancel_chain`](../../hermes_cli/kanban_db.py#L12334) | CLI | final/already-blocked members are skipped | result lists members under `skipped` |
| running\|ready\|blocked → ready | [`_advance_workflow_step`](../../hermes_cli/kanban_db.py#L15013) | worker / workflow gate | no valid next step, stale run ID, wrong status | task completes normally or update returns false |
| running → review | [`_maybe_advance_review_chain`](../../hermes_cli/kanban_db.py#L15177) | review gate | verdict must be approved, workflow identity valid, another stage exists | identity failure blocks; final stage proceeds to `done` |
| running\|ready\|blocked → done | [`complete_task`](../../hermes_cli/kanban_db.py#L15485) | worker / CLI / review gate | hallucinated cards, worker/review gate, stale run, integration park | exception/event or card moves to `review`/`blocked` instead |
| running → ready\|review | [`release_stale_claims`](../../hermes_cli/kanban_db.py#L11953) | dispatcher daemon | live local PID plus fresh heartbeat extends claim; surviving process prevents release | `claim_extended` or `reclaim_deferred`; card remains `running` |
| running\|ready\|blocked → ready | [`reclaim_task`](../../hermes_cli/kanban_db.py#L12136) | CLI | a worker process group that survives termination | `reclaim_deferred`; claim stays owned |
| blocked\|scheduled → todo\|ready | [`unblock_task`](../../hermes_cli/kanban_db.py#L18180) | CLI / automation | typed wait unless audited override; unfinished parents route to `todo` | unblock returns false or succeeds into `todo` |
| todo\|ready\|running\|blocked → scheduled | [`schedule_task`](../../hermes_cli/kanban_db.py#L20121) | CLI / cron / worker | active typed wait, stale expected run ID | schedule returns false; previous status remains |
| any non-archived → archived | [`archive_task`](../../hermes_cli/kanban_db.py#L19367) | CLI / sweep | dependent wait, changed ownership generation, surviving worker | archive refuses; wait conflict or `reclaim_deferred` is visible |

## Dispatch path: tick to worker process

1. [`dispatch_once`](../../hermes_cli/kanban_db.py#L28760) resolves the board DB
   and enters [`_dispatch_tick_lock`](../../hermes_cli/kanban_db.py#L2775), a
   non-blocking OS lock on the DB-adjacent `.dispatch.lock`. A loser returns
   `skipped_locked=True` and performs no tick writes.
2. [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py#L28847) reaps zombies
   and pending continuations, refreshes Claude-CLI heartbeats through
   [`heartbeat_live_claude_cli_workers`](../../hermes_cli/kanban_db.py#L21308),
   reclaims stale/dead/timed-out runs, optionally retries settled blocks, then
   calls [`recompute_ready`](../../hermes_cli/kanban_db.py#L11165).
3. It selects unclaimed `ready` rows by priority and age. Before claim it applies
   global and per-profile concurrency, assignee spawnability, repo/chain/writer
   serialization, daily budget, G1 cumulative input tokens, role-fit, code
   contract, and [`check_respawn_guard`](../../hermes_cli/kanban_db.py#L27072).
   Advisory holds leave the row `ready`; [`summarize_dispatch_holds`](../../hermes_cli/kanban_db.py#L27312)
   groups the operator-visible buckets.
4. [`claim_task`](../../hermes_cli/kanban_db.py#L11305) performs the
   `ready → running` CAS, stamps claim expiry and immutable route identity, and
   creates the run. Review rows take the parallel
   [`claim_review_task`](../../hermes_cli/kanban_db.py#L11544) path.
5. [`_resolve_dispatch_workspace`](../../hermes_cli/kanban_db.py#L20011)
   chooses managed worktree provisioning or existing-workspace resolution.
   [`resolve_workspace`](../../hermes_cli/kanban_db.py#L19917) defines the
   scratch, absolute directory, and linked-worktree behavior. Base-preparation
   drift/conflict is checked before the materialized path is persisted.
6. A writable shared chain worktree acquires a writer lease. Then
   [`_default_spawn`](../../hermes_cli/kanban_db.py#L31244) freezes the worker
   environment and launch route, and [`_launch_worker_process`](../../hermes_cli/kanban_db.py#L30605)
   starts a new-session subprocess with its per-task log. The PID is attached
   only if the claim generation is still current.

### What the spawned worker sees

[`build_worker_context`](../../hermes_cli/kanban_db.py#L32691) returns the
canonical phase-aware bounded brief built by
[`render_worker_brief_for_task`](../../hermes_cli/kanban_db.py#L32652). It
contains task/workflow identity, assignee, status, tenant, materialized
workspace/branch, task body and expanded scope contract, knowledge pointers,
attachments, continuation notice, parent results, reviewer findings, comments,
prior runs, same-tenant role history (not for scouts), and the immutable review
diff when applicable. Oversized sections become hashed per-run artifacts.

[`_default_spawn`](../../hermes_cli/kanban_db.py#L31244) additionally pins the
task ID, workspace, run ID, claim lock, board DB/slug/workspace root, profile,
tenant, branch, iteration/runtime controls, and terminal working directory in
the child environment. The worker must end by calling the task-scoped complete
or block lifecycle command; a clean exit without either is a protocol violation.

### Heartbeats

Workers update task and run liveness through
[`heartbeat_worker`](../../hermes_cli/kanban_db.py#L21124); Claude-CLI lanes are
bridged by [`heartbeat_live_claude_cli_workers`](../../hermes_cli/kanban_db.py#L21308).
The daemon publishes board counts and tick health through
[`write_kanban_dispatcher_heartbeat`](../../hermes_cli/kanban_db.py#L27017).
An expired claim with a live local PID is extended only while observable
heartbeat progress remains fresh.

## Stall and failure modes

| mode | trigger | enforcing code | confirm live | clear |
|---|---|---|---|---|
| Dispatch tick lock | another process owns the board lock, or lock setup cannot prove ownership | [`_dispatch_tick_lock`](../../hermes_cli/kanban_db.py#L2775) | tick result has `skipped_locked=True`; no maintenance/spawn writes from that tick | stop the duplicate dispatcher or repair lock-path access; next tick retries automatically |
| Respawn cooldown/duplicate-work guard | recent rate limit/transient retry/auth failure/success, active PR, or invalid code contract | [`check_respawn_guard`](../../hermes_cli/kanban_db.py#L27072) | card remains `ready`; deduped `respawn_guarded` event names the reason | wait for cooldown, correct auth/contract, close or deliberately requeue after successful work, resolve the PR |
| G1 cumulative input-token runaway | all-run input sum exceeds configured per-task cap; one actionable review extension may be allowed | [`_park_budget_runaway`](../../hermes_cli/kanban_db.py#L24391) | card becomes capacity-blocked; `budget_runaway_parked` and operator-escalation events include sum/cap/run count | inspect the runaway, then operator unblocks/reassigns/closes; raising the cap alone does not change the parked state |
| Dispatch holds | repo, chain-worktree, writer lease, daily budget, role mismatch, or per-profile concurrency is occupied | [`summarize_dispatch_holds`](../../hermes_cli/kanban_db.py#L27312) | card stays `ready`; tick bucket/event names `repo_serialized`, `chain_worktree_serialized`, `worktree_writer_active`, `budget_held`, `role_fit_held`, or profile cap | let holder finish/reclaim, repair stale writer ownership, adjust routing/cap, or remove the conflicting work |
| Review ping-pong breaker | repeated review-origin `REQUEST_CHANGES` reaches configured maximum rounds | [`block_task`](../../hermes_cli/kanban_db.py#L17134) | task is `blocked` as `needs_input`; reason starts `review ping-pong breaker`; operator escalation class is `review_pingpong` | operator resolves findings and explicitly unblocks/respecs/reassigns; it does not auto-retry |
| Parent/wait gate | parent is not `done`, due time is future, or typed wait remains unsatisfied/invalid | [`recompute_ready`](../../hermes_cli/kanban_db.py#L11165) | task remains `todo`/`blocked`; claim may emit `claim_rejected` | finish parent, wait for due/event, or use the audited wait override |
| Global/profile concurrency | live running count reaches board, spawn, or profile cap | [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py#L28847) | no spawn; profile-capped tasks appear in the hold result, global cap returns early | allow running tasks to terminate/reclaim or change the configured cap |
| Nonspawnable assignee | assigned name is not an on-disk Hermes profile | [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py#L28847) | `nonspawnable` event; unknown lanes also emit one operator escalation | assign a real profile or provision the intended profile; terminal pull-only lanes remain intentionally held |
| Workspace provisioning/base drift | invalid path, worktree lock/timeout, rebase conflict, or reused base differs | [`_resolve_dispatch_workspace`](../../hermes_cli/kanban_db.py#L20011) | claimed run is requeued/blocked; events include spawn retry/failure or `worker_base_rejected` | fix path/git state; transient timeouts back off; conflicts use the bounded fixer path or operator repair |
| Claim/heartbeat expiry | TTL expires and no sufficiently fresh observable progress exists | [`release_stale_claims`](../../hermes_cli/kanban_db.py#L11953) | `reclaimed`, `claim_extended`, or `reclaim_deferred` event; status is `ready`, `review`, or still `running` | healthy workers heartbeat; terminate/reclaim a wedged process; never clear ownership while its process survives |
| Worker exits without terminal lifecycle call | subprocess exits zero while task is still running | [`detect_crashed_workers`](../../hermes_cli/kanban_db.py#L21828) | `protocol_violation` or `deliverable_posted_not_completed`; bounded repeats end in `gave_up` | recover posted evidence or rerun with the required complete/block call; operator unblocks after breaker trip |
| Spawn/config failure breaker | model route, executable, workspace, or repeated spawn fails | [`_record_spawn_failure`](../../hermes_cli/kanban_db.py#L23409) | failure counter and spawn events accumulate; terminal attempt becomes blocked/auto-blocked | repair deterministic config; allow bounded transient retry, then explicitly unblock after correction |
| Sticky worker/operator block | latest block or active escalation requires a decision | [`_has_sticky_block`](../../hermes_cli/kanban_db.py#L10980) | card stays `blocked` even when every parent is done | [`unblock_task`](../../hermes_cli/kanban_db.py#L18180) after resolving the stated cause |

## Section index

| line | banner title |
|---:|---|
| 123 | [Constants](../../hermes_cli/kanban_db.py#L123) |
| 532 | [Paths](../../hermes_cli/kanban_db.py#L532) |
| 1216 | [Data classes](../../hermes_cli/kanban_db.py#L1216) |
| 1679 | [Vault / Memory link extraction](../../hermes_cli/kanban_db.py#L1679) |
| 2113 | [Schema](../../hermes_cli/kanban_db.py#L2113) |
| 2526 | [Connection helpers](../../hermes_cli/kanban_db.py#L2526) |
| 4157 | [ID generation](../../hermes_cli/kanban_db.py#L4157) |
| 4200 | [Task creation / mutation](../../hermes_cli/kanban_db.py#L4200) |
| 5959 | [Links](../../hermes_cli/kanban_db.py#L5959) |
| 6316 | [Comments & events](../../hermes_cli/kanban_db.py#L6316) |
| 6403 | [Attachments](../../hermes_cli/kanban_db.py#L6403) |
| 10195 | [Dependency resolution (todo -> ready)](../../hermes_cli/kanban_db.py#L10195) |
| 11301 | [Claim / complete / block](../../hermes_cli/kanban_db.py#L11301) |
| 12645 | [Review gate (Phase 2: independent verification before 'done')](../../hermes_cli/kanban_db.py#L12645) |
| 16100 | [Workspace / tmux cleanup](../../hermes_cli/kanban_db.py#L16100) |
| 16579 | [First-use tip for scratch workspaces](../../hermes_cli/kanban_db.py#L16579) |
| 19637 | [Workspace resolution](../../hermes_cli/kanban_db.py#L19637) |
| 20188 | [Dispatcher (one-shot pass)](../../hermes_cli/kanban_db.py#L20188) |
| 20218 | [Respawn guard constants](../../hermes_cli/kanban_db.py#L20218) |
| 24185 | [G1: per-task cumulative input-token runaway guard](../../hermes_cli/kanban_db.py#L24185) |
| 27527 | [OpenClaw cross-system dispatch (Mission-Control via HMAC-signed envelopes)](../../hermes_cli/kanban_db.py#L27527) |
| 27764 | [B1a — tree-wide inventory/hygiene must not land on research/premium LLM loops](../../hermes_cli/kanban_db.py#L27764) |
| 31557 | [Long-lived dispatcher daemon](../../hermes_cli/kanban_db.py#L31557) |
| 31631 | [Worker context builder (what a spawned worker sees)](../../hermes_cli/kanban_db.py#L31631) |
| 32263 | [Scope-contract template expansion (PlanSpec B)](../../hermes_cli/kanban_db.py#L32263) |
| 32705 | [Stats + SLA helpers](../../hermes_cli/kanban_db.py#L32705) |
| 35429 | [Epics (N-E3) — durable goals spanning multiple task trees](../../hermes_cli/kanban_db.py#L35429) |
| 35603 | [Disposition Ledger (FRD-S1) — additive; no wiring into completion path yet](../../hermes_cli/kanban_db.py#L35603) |
| 36457 | [Lanes (night-sprint F1) — switchable profile→(runtime, model) presets](../../hermes_cli/kanban_db.py#L36457) |
| 36887 | [Notification subscriptions (used by the gateway kanban-notifier)](../../hermes_cli/kanban_db.py#L36887) |
| 37006 | [Browser Web Push subscriptions (used by the control dashboard)](../../hermes_cli/kanban_db.py#L37006) |
| 37408 | [Retention + garbage collection](../../hermes_cli/kanban_db.py#L37408) |
| 37457 | [Worker log accessor](../../hermes_cli/kanban_db.py#L37457) |
| 37506 | [Assignee enumeration (known profiles + per-profile board stats)](../../hermes_cli/kanban_db.py#L37506) |
| 37598 | [Runs (attempt history on a task)](../../hermes_cli/kanban_db.py#L37598) |
| 37646 | [Durable Kanban ↔ TMAX execution capsule](../../hermes_cli/kanban_db.py#L37646) |
