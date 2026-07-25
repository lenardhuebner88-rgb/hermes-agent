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
| creation → triage | [`create_task`](../../hermes_cli/kanban_db.py#L5367) | CLI / automation | invalid initial fields, missing parents, role/workspace contract | no row is created; caller gets a validation error |
| creation → todo | [`create_task`](../../hermes_cli/kanban_db.py#L5367) | CLI / automation | missing parent IDs | new card waits in `todo`; an unfinished parent is visible |
| creation → ready | [`create_task`](../../hermes_cli/kanban_db.py#L5367) | CLI / automation | unfinished parents route to `todo` instead | card is `todo`, not `ready` |
| creation → blocked | [`create_task`](../../hermes_cli/kanban_db.py#L5367) | CLI / automation | only the explicit blocked initial mode is accepted | card is born as an operator park |
| triage → todo | [`specify_triage_task`](../../hermes_cli/kanban_db.py#L18894) | CLI / sweep | row must still be in `triage` | specify returns false; status did not change |
| todo → ready | [`recompute_ready`](../../hermes_cli/kanban_db.py#L11726) | dispatcher daemon / completion sweep | parents, due time, typed wait, sticky block, active escalation, failure limit | card remains `todo` or `blocked`; no `promoted` event |
| todo\|blocked → ready | [`promote_task`](../../hermes_cli/kanban_db.py#L18050) | CLI | typed wait or unfinished parents; blocked tasks cannot force past parents | command reports the refusing parent/wait |
| ready → todo | [`claim_task`](../../hermes_cli/kanban_db.py#L11866) | dispatcher daemon / CLI claimer | active wait or any parent not `done` | `claim_rejected` event; reason is `active_wait` or `parents_not_done` |
| ready → running | [`claim_task`](../../hermes_cli/kanban_db.py#L11866) | dispatcher daemon / CLI claimer | code contract, wait, parents, claim CAS | card stays `ready`; no current run is opened |
| review → running | [`claim_review_task`](../../hermes_cli/kanban_db.py#L12105) | review gate / dispatcher daemon | already claimed, wrong status, unavailable review profile before claim | card stays `review`; review hold event identifies the target |
| running\|ready → blocked | [`block_task`](../../hermes_cli/kanban_db.py#L17709) | worker / CLI / review gate | stale run ID, wrong status, invalid review-origin contract | block returns false; rejection event explains the mismatch |
| running\|ready → todo | [`block_task`](../../hermes_cli/kanban_db.py#L17709) | worker | a valid unsatisfied dependency wait deliberately parks on `todo` | `wait_registered` plus a `blocked` event whose status is `todo` |
| running\|ready → triage | [`block_task`](../../hermes_cli/kanban_db.py#L17709) | worker / sweep | same non-review block kind must reach [`BLOCK_RECURRENCE_LIMIT`](../../hermes_cli/kanban_db.py#L172) | `block_loop_detected` event; card is in `triage` |
| running\|ready\|blocked → review | [`_submit_for_review`](../../hermes_cli/kanban_db.py#L15252) | worker / review gate | workflow identity, worker gate, expected run CAS | task blocks on identity failure or completion raises a worker-gate error |
| running → blocked | [`hold_task`](../../hermes_cli/kanban_db.py#L12792) | CLI | task must still be running | hold returns false; worker/status won the race |
| triage\|todo\|scheduled\|ready\|running\|review → blocked | [`cancel_chain`](../../hermes_cli/kanban_db.py#L12895) | CLI | final/already-blocked members are skipped | result lists members under `skipped` |
| running\|ready\|blocked → ready | [`_advance_workflow_step`](../../hermes_cli/kanban_db.py#L15586) | worker / workflow gate | no valid next step, stale run ID, wrong status | task completes normally or update returns false |
| running → review | [`_maybe_advance_review_chain`](../../hermes_cli/kanban_db.py#L15750) | review gate | verdict must be approved, workflow identity valid, another stage exists | identity failure blocks; final stage proceeds to `done` |
| running\|ready\|blocked → done | [`complete_task`](../../hermes_cli/kanban_db.py#L16058) | worker / CLI / review gate | hallucinated cards, worker/review gate, stale run, integration park | exception/event or card moves to `review`/`blocked` instead |
| running → ready\|review | [`release_stale_claims`](../../hermes_cli/kanban_db.py#L12514) | dispatcher daemon | live local PID plus fresh heartbeat extends claim; surviving process prevents release | `claim_extended` or `reclaim_deferred`; card remains `running` |
| running\|ready\|blocked → ready | [`reclaim_task`](../../hermes_cli/kanban_db.py#L12697) | CLI | a worker process group that survives termination | `reclaim_deferred`; claim stays owned |
| blocked\|scheduled → todo\|ready | [`unblock_task`](../../hermes_cli/kanban_db.py#L18755) | CLI / automation | typed wait unless audited override; unfinished parents route to `todo` | unblock returns false or succeeds into `todo` |
| todo\|ready\|running\|blocked → scheduled | [`schedule_task`](../../hermes_cli/kanban_db.py#L20735) | CLI / cron / worker | active typed wait, stale expected run ID | schedule returns false; previous status remains |
| any non-archived → archived | [`archive_task`](../../hermes_cli/kanban_db.py#L19951) | CLI / sweep | dependent wait, changed ownership generation, surviving worker | archive refuses; wait conflict or `reclaim_deferred` is visible |

## Dispatch path: tick to worker process

1. [`dispatch_once`](../../hermes_cli/kanban_db.py#L29376) resolves the board DB
   and enters [`_dispatch_tick_lock`](../../hermes_cli/kanban_db.py#L2827), a
   non-blocking OS lock on the DB-adjacent `.dispatch.lock`. A loser returns
   `skipped_locked=True` and performs no tick writes.
2. [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py#L29467) reaps zombies
   and pending continuations, refreshes Claude-CLI heartbeats through
   [`heartbeat_live_claude_cli_workers`](../../hermes_cli/kanban_db.py#L21924),
   reclaims stale/dead/timed-out runs, optionally retries settled blocks, then
   calls [`recompute_ready`](../../hermes_cli/kanban_db.py#L11726).
3. It selects unclaimed `ready` rows by priority and age. Before claim it applies
   global and per-profile concurrency, assignee spawnability, repo/chain/writer
   serialization, daily budget, G1 cumulative input tokens, role-fit, code
   contract, and [`check_respawn_guard`](../../hermes_cli/kanban_db.py#L27688).
   Advisory holds leave the row `ready`; [`summarize_dispatch_holds`](../../hermes_cli/kanban_db.py#L27928)
   groups the operator-visible buckets.
4. [`claim_task`](../../hermes_cli/kanban_db.py#L11866) performs the
   `ready → running` CAS, stamps claim expiry and immutable route identity, and
   creates the run. Review rows take the parallel
   [`claim_review_task`](../../hermes_cli/kanban_db.py#L12105) path.
5. [`_resolve_dispatch_workspace`](../../hermes_cli/kanban_db.py#L20625)
   chooses managed worktree provisioning or existing-workspace resolution.
   [`resolve_workspace`](../../hermes_cli/kanban_db.py#L20531) defines the
   scratch, absolute directory, and linked-worktree behavior. Base-preparation
   drift/conflict is checked before the materialized path is persisted.
6. A writable shared chain worktree acquires a writer lease. Then
   [`_default_spawn`](../../hermes_cli/kanban_db.py#L31870) freezes the worker
   environment and launch route, and [`_launch_worker_process`](../../hermes_cli/kanban_db.py#L31231)
   starts a new-session subprocess with its per-task log. The PID is attached
   only if the claim generation is still current.

### Model and provider route: stamped at claim, frozen at spawn

This is the single most consequential mechanic in the dispatch path and the one
most likely to mislead. The worker's model/provider is **not** read from the
task row at spawn time. It is resolved once at *claim* time and frozen.

1. [`claim_task`](../../hermes_cli/kanban_db.py#L11866) (and
   [`claim_review_task`](../../hermes_cli/kanban_db.py#L12105)) call
   [`_spawn_identity_metadata`](../../hermes_cli/kanban_db.py#L8050), which
   resolves model and provider and persists them on the run.
2. [`_default_spawn`](../../hermes_cli/kanban_db.py#L31870) reads that stamp back
   via [`_persisted_spawn_identity`](../../hermes_cli/kanban_db.py#L31663) and
   sets its local `route_is_frozen` flag from it. Resolving the
   stamp *before* consulting the mutable lane is deliberate: a lane edited
   between claim and launch must not be able to change the argv of a run already
   in flight.

Precedence inside the stamp — first match wins:

| # | source | `model_source` | how it is expressed |
|---|---|---|---|
| 1 | per-task override + explicit provider | `task_override_with_provider_switch` | `model_override` + `provider_override` (two columns), **or** a single `provider/model` string in `model_override` |
| 2 | per-task override, provider left alone | `task_override` | `model_override` only; stays on the lane/profile provider |
| 3 | active lane | `lane` | lane `model` / `provider` |
| 4 | assignee profile config | `profile` | no override anywhere |

Both spellings in row 1 resolve identically —
[`_resolve_model_override`](../../hermes_cli/kanban_db.py#L7913) is the single
resolver, and
`tests/hermes_cli/test_kanban_provider_override_dispatch_fork.py` pins that
equivalence. An override whose model belongs to a *different* provider family
than the resolved provider is a poison pill: it is refused by
[`_handle_incompatible_model_override`](../../hermes_cli/kanban_db.py#L8027),
the stamp carries no model at all, and the spawn then fails closed with
`no concrete model route` rather than quietly running on the profile default.

Resulting argv (built in `_default_spawn`): `-m <model>` and
`--provider <name>` are placed **after** the `chat` subcommand. Before `chat`
the chat subparser's own `default=None` clobbers the value in the shared
namespace and the override silently never reaches the worker — the same
reasoning applies to `--max-turns`.

> Consequence worth internalising: because a really-claimed task always has a
> stamp, `route_is_frozen` is true for every dispatched task, so any code that
> reads the *mutable* task row for routing at spawn time is unreachable. The
> `elif task.provider_override and not route_is_frozen` branch inside
> [`_default_spawn`](../../hermes_cli/kanban_db.py#L31870) is exactly that — it
> is kept byte-identical to upstream for merge-cost reasons, not because it
> fires. Fixes to routing belong in the stamp. Editing `model_override` on an
> already-running task has no effect until the next claim, with no warning.

### What the spawned worker sees

[`build_worker_context`](../../hermes_cli/kanban_db.py#L33327) returns the
canonical phase-aware bounded brief built by
[`render_worker_brief_for_task`](../../hermes_cli/kanban_db.py#L33288). It
contains task/workflow identity, assignee, status, tenant, materialized
workspace/branch, task body and expanded scope contract, knowledge pointers,
attachments, continuation notice, parent results, reviewer findings, comments,
prior runs, same-tenant role history (not for scouts), and the immutable review
diff when applicable. Oversized sections become hashed per-run artifacts.

[`_default_spawn`](../../hermes_cli/kanban_db.py#L31870) additionally pins the
task ID, workspace, run ID, claim lock, board DB/slug/workspace root, profile,
tenant, branch, iteration/runtime controls, and terminal working directory in
the child environment. The worker must end by calling the task-scoped complete
or block lifecycle command; a clean exit without either is a protocol violation.

### Heartbeats

Workers update task and run liveness through
[`heartbeat_worker`](../../hermes_cli/kanban_db.py#L21740); Claude-CLI lanes are
bridged by [`heartbeat_live_claude_cli_workers`](../../hermes_cli/kanban_db.py#L21924).
The daemon publishes board counts and tick health through
[`write_kanban_dispatcher_heartbeat`](../../hermes_cli/kanban_db.py#L27633).
An expired claim with a live local PID is extended only while observable
heartbeat progress remains fresh.

## Stall and failure modes

| mode | trigger | enforcing code | confirm live | clear |
|---|---|---|---|---|
| Dispatch tick lock | another process owns the board lock, or lock setup cannot prove ownership | [`_dispatch_tick_lock`](../../hermes_cli/kanban_db.py#L2827) | tick result has `skipped_locked=True`; no maintenance/spawn writes from that tick | stop the duplicate dispatcher or repair lock-path access; next tick retries automatically |
| Respawn cooldown/duplicate-work guard | recent rate limit/transient retry/auth failure/success, active PR, or invalid code contract | [`check_respawn_guard`](../../hermes_cli/kanban_db.py#L27688) | card remains `ready`; deduped `respawn_guarded` event names the reason | wait for cooldown, correct auth/contract, close or deliberately requeue after successful work, resolve the PR |
| G1 cumulative input-token runaway | all-run input sum exceeds configured per-task cap; one actionable review extension may be allowed | [`_park_budget_runaway`](../../hermes_cli/kanban_db.py#L25007) | card becomes capacity-blocked; `budget_runaway_parked` and operator-escalation events include sum/cap/run count | inspect the runaway, then operator unblocks/reassigns/closes; raising the cap alone does not change the parked state |
| Dispatch holds | repo, chain-worktree, writer lease, daily budget, role mismatch, or per-profile concurrency is occupied | [`summarize_dispatch_holds`](../../hermes_cli/kanban_db.py#L27928) | card stays `ready`; tick bucket/event names `repo_serialized`, `chain_worktree_serialized`, `worktree_writer_active`, `budget_held`, `role_fit_held`, or profile cap | let holder finish/reclaim, repair stale writer ownership, adjust routing/cap, or remove the conflicting work |
| Review ping-pong breaker | repeated review-origin `REQUEST_CHANGES` reaches configured maximum rounds | [`block_task`](../../hermes_cli/kanban_db.py#L17709) | task is `blocked` as `needs_input`; reason starts `review ping-pong breaker`; operator escalation class is `review_pingpong` | operator resolves findings and explicitly unblocks/respecs/reassigns; it does not auto-retry |
| Parent/wait gate | parent is not `done`, due time is future, or typed wait remains unsatisfied/invalid | [`recompute_ready`](../../hermes_cli/kanban_db.py#L11726) | task remains `todo`/`blocked`; claim may emit `claim_rejected` | finish parent, wait for due/event, or use the audited wait override |
| Global/profile concurrency | live running count reaches board, spawn, or profile cap | [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py#L29467) | no spawn; profile-capped tasks appear in the hold result, global cap returns early | allow running tasks to terminate/reclaim or change the configured cap |
| Nonspawnable assignee | assigned name is not an on-disk Hermes profile | [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py#L29467) | `nonspawnable` event; unknown lanes also emit one operator escalation | assign a real profile or provision the intended profile; terminal pull-only lanes remain intentionally held |
| Workspace provisioning/base drift | invalid path, worktree lock/timeout, rebase conflict, or reused base differs | [`_resolve_dispatch_workspace`](../../hermes_cli/kanban_db.py#L20625) | claimed run is requeued/blocked; events include spawn retry/failure or `worker_base_rejected` | fix path/git state; transient timeouts back off; conflicts use the bounded fixer path or operator repair |
| Claim/heartbeat expiry | TTL expires and no sufficiently fresh observable progress exists | [`release_stale_claims`](../../hermes_cli/kanban_db.py#L12514) | `reclaimed`, `claim_extended`, or `reclaim_deferred` event; status is `ready`, `review`, or still `running` | healthy workers heartbeat; terminate/reclaim a wedged process; never clear ownership while its process survives |
| Worker exits without terminal lifecycle call | subprocess exits zero while task is still running | [`detect_crashed_workers`](../../hermes_cli/kanban_db.py#L22444) | `protocol_violation` or `deliverable_posted_not_completed`; bounded repeats end in `gave_up` | recover posted evidence or rerun with the required complete/block call; operator unblocks after breaker trip |
| Spawn/config failure breaker | model route, executable, workspace, or repeated spawn fails | [`_record_spawn_failure`](../../hermes_cli/kanban_db.py#L24025) | failure counter and spawn events accumulate; terminal attempt becomes blocked/auto-blocked | repair deterministic config; allow bounded transient retry, then explicitly unblock after correction |
| Sticky worker/operator block | latest block or active escalation requires a decision | [`_has_sticky_block`](../../hermes_cli/kanban_db.py#L11541) | card stays `blocked` even when every parent is done | [`unblock_task`](../../hermes_cli/kanban_db.py#L18755) after resolving the stated cause |

## Traps

Each of these has cost a real session real time. They are invisible from the
state diagram.

**`HERMES_HOME` does not isolate the board.** Board paths resolve through
[`kanban_home`](../../hermes_cli/kanban_db.py#L619) /
[`kanban_db_path`](../../hermes_cli/kanban_db.py#L852), which anchor to the
shared hermes root on purpose — otherwise every profile would silently fork its
own board and break the dispatcher/worker handoff. So a test or probe that sets
only `HERMES_HOME` still reads and **migrates the live `kanban.db`**. Set
`HERMES_KANBAN_HOME` as well for a genuinely isolated run. This has leaked
production rows at least twice.

**The route is frozen at claim.** See the route section above. Anything reading
the mutable task row for routing at spawn time is dead code.

**`connect()`'s fast path skips the integrity guard.** Once a board carries the
current schema stamp, [`_try_fast_connect`](../../hermes_cli/kanban_db.py#L2703)
compares `PRAGMA user_version` and returns;
[`_guard_existing_db_is_healthy`](../../hermes_cli/kanban_db.py#L3213) runs only
on the cold init path in [`connect`](../../hermes_cli/kanban_db.py#L3431) or when
`hermes kanban repair` is invoked explicitly. That is a deliberate trade — it
removed a 120 s serialization — but it means silent index corruption is no
longer detected at connect. `hermes kanban repair` is the antidote. Two tests
are red *by design* because of this; see
`docs/refactor/upstream-backlog.kanban_db.md` §3 before "fixing" them.

**Two model flags exist in one argv.** A top-level `--model` and the
chat-subcommand `-m` are computed by different fallback chains in
`_default_spawn`. When debugging "the worker used the wrong model", check both.

**`_record_spawn_failure` is a wrapper, not the engine.** The counter and
breaker logic lives in
[`_record_task_failure`](../../hermes_cli/kanban_db.py#L23617);
[`_record_spawn_failure`](../../hermes_cli/kanban_db.py#L24025) delegates to it.
Grep finds the old name first, so a change applied only there misses the crash
and timeout paths that call `_record_task_failure` directly.

**This file's anchors rot silently.** Every anchor is a line number into a
~39 k-line file, so any edit to the monolith can invalidate the whole map while
the links still *look* precise. `scripts/check_kanban_lifecycle_anchors.py`
detects it and `--fix` repairs it; `tests/scripts/test_check_kanban_lifecycle_anchors.py`
enforces it, and `scripts/affected_tests.py` maps `hermes_cli/kanban_db.py` to
that test so an edit to the monolith runs it. Before 2026-07-25 no pattern
reached it and 92 anchors had drifted unnoticed.

## Other entry points into the lifecycle

The transition table starts at
[`create_task`](../../hermes_cli/kanban_db.py#L5367) because that is the common
path, but it is not the only one. Tasks also enter the board through epic/triage
fan-out (`hermes_cli/kanban_decompose.py`), PlanSpec ingest
(`hermes_cli/planspecs.py`, plus `hermes_cli/pa_planspec.py` for drafts), and
`create_epic`, which maintains a parallel `open`/closed object referenced by
`tasks.epic_id`. All of them funnel into the same state vocabulary, so the
transition table still applies once the row exists.

## Section index

| line | banner title |
|---:|---|
| 123 | [Constants](../../hermes_cli/kanban_db.py#L123) |
| 532 | [Paths](../../hermes_cli/kanban_db.py#L555) |
| 1216 | [Data classes](../../hermes_cli/kanban_db.py#L1243) |
| 1679 | [Vault / Memory link extraction](../../hermes_cli/kanban_db.py#L1718) |
| 2113 | [Schema](../../hermes_cli/kanban_db.py#L2152) |
| 2526 | [Connection helpers](../../hermes_cli/kanban_db.py#L2570) |
| 4157 | [ID generation](../../hermes_cli/kanban_db.py#L4530) |
| 4200 | [Task creation / mutation](../../hermes_cli/kanban_db.py#L4573) |
| 5959 | [Links](../../hermes_cli/kanban_db.py#L6423) |
| 6316 | [Comments & events](../../hermes_cli/kanban_db.py#L6780) |
| 6403 | [Attachments](../../hermes_cli/kanban_db.py#L6867) |
| 10195 | [Dependency resolution (todo -> ready)](../../hermes_cli/kanban_db.py#L10756) |
| 11301 | [Claim / complete / block](../../hermes_cli/kanban_db.py#L11862) |
| 12645 | [Review gate (Phase 2: independent verification before 'done')](../../hermes_cli/kanban_db.py#L13206) |
| 16100 | [Workspace / tmux cleanup](../../hermes_cli/kanban_db.py#L16673) |
| 16579 | [First-use tip for scratch workspaces](../../hermes_cli/kanban_db.py#L17154) |
| 19637 | [Workspace resolution](../../hermes_cli/kanban_db.py#L20221) |
| 20188 | [Dispatcher (one-shot pass)](../../hermes_cli/kanban_db.py#L20802) |
| 20218 | [Respawn guard constants](../../hermes_cli/kanban_db.py#L20832) |
| 24185 | [G1: per-task cumulative input-token runaway guard](../../hermes_cli/kanban_db.py#L24801) |
| 27527 | [OpenClaw cross-system dispatch (Mission-Control via HMAC-signed envelopes)](../../hermes_cli/kanban_db.py#L28143) |
| 27764 | [B1a — tree-wide inventory/hygiene must not land on research/premium LLM loops](../../hermes_cli/kanban_db.py#L28380) |
| 31557 | [Long-lived dispatcher daemon](../../hermes_cli/kanban_db.py#L32191) |
| 31631 | [Worker context builder (what a spawned worker sees)](../../hermes_cli/kanban_db.py#L32265) |
| 32263 | [Scope-contract template expansion (PlanSpec B)](../../hermes_cli/kanban_db.py#L32897) |
| 32705 | [Stats + SLA helpers](../../hermes_cli/kanban_db.py#L33341) |
| 35429 | [Epics (N-E3) — durable goals spanning multiple task trees](../../hermes_cli/kanban_db.py#L36074) |
| 35603 | [Disposition Ledger (FRD-S1) — additive; no wiring into completion path yet](../../hermes_cli/kanban_db.py#L36248) |
| 36457 | [Lanes (night-sprint F1) — switchable profile→(runtime, model) presets](../../hermes_cli/kanban_db.py#L37157) |
| 36887 | [Notification subscriptions (used by the gateway kanban-notifier)](../../hermes_cli/kanban_db.py#L37587) |
| 37006 | [Browser Web Push subscriptions (used by the control dashboard)](../../hermes_cli/kanban_db.py#L37706) |
| 37408 | [Retention + garbage collection](../../hermes_cli/kanban_db.py#L38108) |
| 37457 | [Worker log accessor](../../hermes_cli/kanban_db.py#L38157) |
| 37506 | [Assignee enumeration (known profiles + per-profile board stats)](../../hermes_cli/kanban_db.py#L38206) |
| 37598 | [Runs (attempt history on a task)](../../hermes_cli/kanban_db.py#L38298) |
| 37646 | [Durable Kanban ↔ TMAX execution capsule](../../hermes_cli/kanban_db.py#L38346) |
