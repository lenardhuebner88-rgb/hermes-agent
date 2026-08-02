# Kanban task lifecycle map

`hermes_cli/kanban_db.py` is larger than 1 MiB, so CodeGraph does not index it.
Use `rg` plus this map, not CodeGraph results for similarly named test doubles.
Top-level symbol and exact banner anchors are verified by
`scripts/check_kanban_lifecycle_anchors.py`.

Source of truth for the state vocabulary:
[`VALID_STATUSES`](../../hermes_cli/kanban_db.py).

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
| creation → triage | [`create_task`](../../hermes_cli/kanban_db.py) | CLI / automation | invalid initial fields, missing parents, role/workspace contract | no row is created; caller gets a validation error |
| creation → todo | [`create_task`](../../hermes_cli/kanban_db.py) | CLI / automation | missing parent IDs | new card waits in `todo`; an unfinished parent is visible |
| creation → ready | [`create_task`](../../hermes_cli/kanban_db.py) | CLI / automation | unfinished parents route to `todo` instead | card is `todo`, not `ready` |
| creation → blocked | [`create_task`](../../hermes_cli/kanban_db.py) | CLI / automation | only the explicit blocked initial mode is accepted | card is born as an operator park |
| triage → todo | [`specify_triage_task`](../../hermes_cli/kanban_db.py) | CLI / sweep | row must still be in `triage` | specify returns false; status did not change |
| todo → ready | [`recompute_ready`](../../hermes_cli/kanban_db.py) | dispatcher daemon / completion sweep | parents, due time, typed wait, sticky block, active escalation, failure limit | card remains `todo` or `blocked`; no `promoted` event |
| todo\|blocked → ready | [`promote_task`](../../hermes_cli/kanban_db.py) | CLI | typed wait or unfinished parents; blocked tasks cannot force past parents | command reports the refusing parent/wait |
| ready → todo | [`claim_task`](../../hermes_cli/kanban_db.py) | dispatcher daemon / CLI claimer | active wait or any parent not `done` | `claim_rejected` event; reason is `active_wait` or `parents_not_done` |
| ready → running | [`claim_task`](../../hermes_cli/kanban_db.py) | dispatcher daemon / CLI claimer | code contract, wait, parents, claim CAS | card stays `ready`; no current run is opened |
| review → running | [`claim_review_task`](../../hermes_cli/kanban_db.py) | review gate / dispatcher daemon | already claimed, wrong status, unavailable review profile before claim | card stays `review`; review hold event identifies the target |
| running\|ready → blocked | [`block_task`](../../hermes_cli/kanban_db.py) | worker / CLI / review gate | stale run ID, wrong status, invalid review-origin contract | block returns false; rejection event explains the mismatch |
| running\|ready → todo | [`block_task`](../../hermes_cli/kanban_db.py) | worker | a valid unsatisfied dependency wait deliberately parks on `todo` | `wait_registered` plus a `blocked` event whose status is `todo` |
| running\|ready → triage | [`block_task`](../../hermes_cli/kanban_db.py) | worker / sweep | same non-review block kind must reach [`BLOCK_RECURRENCE_LIMIT`](../../hermes_cli/kanban_db.py) | `block_loop_detected` event; card is in `triage` |
| running\|ready\|blocked → review | [`_submit_for_review`](../../hermes_cli/kanban_db.py) | worker / review gate | workflow identity, worker gate, expected run CAS | task blocks on identity failure or completion raises a worker-gate error |
| running → blocked | [`hold_task`](../../hermes_cli/kanban_db.py) | CLI | task must still be running | hold returns false; worker/status won the race |
| triage\|todo\|scheduled\|ready\|running\|review → blocked | [`cancel_chain`](../../hermes_cli/kanban_db.py) | CLI | final/already-blocked members are skipped | result lists members under `skipped` |
| running\|ready\|blocked → ready | [`_advance_workflow_step`](../../hermes_cli/kanban_db.py) | worker / workflow gate | no valid next step, stale run ID, wrong status | task completes normally or update returns false |
| running → review | [`_maybe_advance_review_chain`](../../hermes_cli/kanban_db.py) | review gate | verdict must be approved, workflow identity valid, another stage exists | identity failure blocks; final stage proceeds to `done` |
| running\|ready\|blocked → done | [`complete_task`](../../hermes_cli/kanban_db.py) | worker / CLI / review gate | hallucinated cards, worker/review gate, stale run, integration park | exception/event or card moves to `review`/`blocked` instead |
| running → ready\|review | [`release_stale_claims`](../../hermes_cli/kanban_db.py) | dispatcher daemon | live local PID plus fresh heartbeat extends claim; surviving process prevents release | `claim_extended` or `reclaim_deferred`; card remains `running` |
| running\|ready\|blocked → ready | [`reclaim_task`](../../hermes_cli/kanban_db.py) | CLI | a worker process group that survives termination | `reclaim_deferred`; claim stays owned |
| blocked\|scheduled → todo\|ready | [`unblock_task`](../../hermes_cli/kanban_db.py) | CLI / automation | typed wait unless audited override; unfinished parents route to `todo` | unblock returns false or succeeds into `todo` |
| todo\|ready\|running\|blocked → scheduled | [`schedule_task`](../../hermes_cli/kanban_db.py) | CLI / cron / worker | active typed wait, stale expected run ID | schedule returns false; previous status remains |
| any non-archived → archived | [`archive_task`](../../hermes_cli/kanban_db.py) | CLI / sweep | dependent wait, changed ownership generation, surviving worker | archive refuses; wait conflict or `reclaim_deferred` is visible. Operator archives (CLI + dashboard, `retrigger_integration=True`) also re-run chain integration after a successful archive — synchronous gate+merge in the caller, see `maybe_retrigger_integration_after_archive` |

## Dispatch path: tick to worker process

1. [`dispatch_once`](../../hermes_cli/kanban_db.py) resolves the board DB
   and enters [`_dispatch_tick_lock`](../../hermes_cli/kanban_db.py), a
   non-blocking OS lock on the DB-adjacent `.dispatch.lock`. A loser returns
   `skipped_locked=True` and performs no tick writes.
2. [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py) reaps zombies
   and pending continuations, refreshes Claude-CLI heartbeats through
   [`heartbeat_live_claude_cli_workers`](../../hermes_cli/kanban_db.py),
   reclaims stale/dead/timed-out runs, optionally retries settled blocks, then
   calls [`recompute_ready`](../../hermes_cli/kanban_db.py).
3. It selects unclaimed `ready` rows by priority and age. Before claim it applies
   global and per-profile concurrency, assignee spawnability, repo/chain/writer
   serialization, daily budget, G1 cumulative input tokens, role-fit, code
   contract, and [`check_respawn_guard`](../../hermes_cli/kanban_db.py).
   Advisory holds leave the row `ready`; [`summarize_dispatch_holds`](../../hermes_cli/kanban_db.py)
   groups the operator-visible buckets.
4. [`claim_task`](../../hermes_cli/kanban_db.py) performs the
   `ready → running` CAS, stamps claim expiry and immutable route identity, and
   creates the run. Review rows take the parallel
   [`claim_review_task`](../../hermes_cli/kanban_db.py) path.
5. [`_resolve_dispatch_workspace`](../../hermes_cli/kanban_db.py)
   chooses managed worktree provisioning or existing-workspace resolution.
   [`resolve_workspace`](../../hermes_cli/kanban_db.py) defines the
   scratch, absolute directory, and linked-worktree behavior. Base-preparation
   drift/conflict is checked before the materialized path is persisted.
6. A writable shared chain worktree acquires a writer lease. Then
   [`_default_spawn`](../../hermes_cli/kanban_db.py) freezes the worker
   environment and launch route, and [`_launch_worker_process`](../../hermes_cli/kanban_db.py)
   starts a new-session subprocess with its per-task log. The PID is attached
   only if the claim generation is still current.

### Model and provider route: stamped at claim, frozen at spawn

This is the single most consequential mechanic in the dispatch path and the one
most likely to mislead. The worker's model/provider is **not** read from the
task row at spawn time. It is resolved once at *claim* time and frozen.

1. [`claim_task`](../../hermes_cli/kanban_db.py) (and
   [`claim_review_task`](../../hermes_cli/kanban_db.py)) call
   [`_spawn_identity_metadata`](../../hermes_cli/kanban_db.py), which
   resolves model and provider and persists them on the run.
2. [`_default_spawn`](../../hermes_cli/kanban_db.py) reads that stamp back
   via [`_persisted_spawn_identity`](../../hermes_cli/kanban_db.py) and
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
[`_resolve_model_override`](../../hermes_cli/kanban_db.py) is the single
resolver, and
`tests/hermes_cli/test_kanban_provider_override_dispatch_fork.py` pins that
equivalence. An override whose model belongs to a *different* provider family
than the resolved provider is a poison pill: it is refused by
[`_handle_incompatible_model_override`](../../hermes_cli/kanban_db.py),
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
> [`_default_spawn`](../../hermes_cli/kanban_db.py) is exactly that — it
> is kept byte-identical to upstream for merge-cost reasons, not because it
> fires. Fixes to routing belong in the stamp. Editing `model_override` on an
> already-running task has no effect until the next claim, with no warning.

### What the spawned worker sees

[`build_worker_context`](../../hermes_cli/kanban_db.py) returns the
canonical phase-aware bounded brief built by
[`render_worker_brief_for_task`](../../hermes_cli/kanban_db.py). It
contains task/workflow identity, assignee, status, tenant, materialized
workspace/branch, task body and expanded scope contract, knowledge pointers,
attachments, continuation notice, parent results, reviewer findings, comments,
prior runs, same-tenant role history (not for scouts), and the immutable review
diff when applicable. Oversized sections become hashed per-run artifacts.

[`_default_spawn`](../../hermes_cli/kanban_db.py) additionally pins the
task ID, workspace, run ID, claim lock, board DB/slug/workspace root, profile,
tenant, branch, iteration/runtime controls, and terminal working directory in
the child environment. The worker must end by calling the task-scoped complete
or block lifecycle command; a clean exit without either is a protocol violation.

### Heartbeats

Workers update task and run liveness through
[`heartbeat_worker`](../../hermes_cli/kanban_db.py); Claude-CLI lanes are
bridged by [`heartbeat_live_claude_cli_workers`](../../hermes_cli/kanban_db.py).
The daemon publishes board counts and tick health through
[`write_kanban_dispatcher_heartbeat`](../../hermes_cli/kanban_db.py).
An expired claim with a live local PID is extended only while observable
heartbeat progress remains fresh.

## Landing path: review verdict to merged branch

The dispatch half above ends at `running`. This half covers everything between a
worker saying "done" and the code actually being on the live branch. It lives in
a **different file** — [`hermes_cli/kanban_worktrees.py`](../../hermes_cli/kanban_worktrees.py) —
which is why it is easy to miss when reading the monolith alone.

### Order of decisions inside `complete_task`

[`complete_task`](../../hermes_cli/kanban_db.py) is a funnel, not a
single write. The order matters and is load-bearing:

1. **Workflow step routing** — a non-final workflow step re-queues instead of
   completing.
2. **Review gate** — [`_review_gate_should_apply`](../../hermes_cli/kanban_db.py):
   gate enabled, this run did *not* originate from review (anti-loop), and the
   assignee is in `code_roles`. Anything else falls straight through to done.
3. **Token levers** — [`_deterministic_review_skip`](../../hermes_cli/kanban_db.py)
   and [`_tip_defer_review`](../../hermes_cli/kanban_db.py) may skip the
   LLM review; otherwise [`_submit_for_review`](../../hermes_cli/kanban_db.py)
   parks the task in `review`.
4. **Review verdict authority** — a review-originated run must carry a
   machine-readable verdict or `ReviewVerdictRequiredError` is raised;
   `REQUEST_CHANGES` routes to `block_task`.
5. **Stage advance** — [`_maybe_advance_review_chain`](../../hermes_cli/kanban_db.py)
   re-parks an APPROVED *intermediate* stage for the next reviewer. Placed
   before integration so a mid-chain stage never triggers a premature merge.
6. **Integration hook** — `maybe_integrate_on_complete`, guarded so it only runs
   for a completion the done-UPDATE would actually accept (same status set, same
   `expected_run_id`). Without that guard a stale worker or a CLI `complete` on a
   `review` row would merge an unreviewed chain.
7. **The done UPDATE.**

Before integration, provisioned code lanes also pass
`_enforce_lane_scope_on_complete`.
The check starts with the review/workspace snapshot diff. Task-local commit
receipts from
`_lane_scope_recorded_task_commit_paths`
may narrow that diff only when they provide attributable single-parent commit
paths. A merge commit, an empty path set, or an unusable receipt means
*attribution unknown*, not "this task changed nothing", so the full snapshot
diff remains in force. Completed fixer children can contribute only their
declared or offending-path allowlist; they do not erase unrelated chain
collateral. This makes a concrete `scope_files` declaration the effective
contract for every code slice.

### The serialized integrator

[`integrate_chain`](../../hermes_cli/kanban_worktrees.py) is *the* single
merge point and it never pushes. It holds a file lock in the repo's `.git` dir
(invisible to `git status`), then runs these stages, each of which can park:

| # | stage | anchored | parks when |
|---|---|---|---|
| 0 | live-checkout precheck | [`_integrate_precheck_live`](../../hermes_cli/kanban_worktrees.py) | `MERGE_HEAD`/rebase in progress, or checked-out branch ≠ frozen merge target |
| 1 | artifact preservation | [`_preserve_or_park_chain_artifacts`](../../hermes_cli/kanban_worktrees.py) | chain worktree dirty with non-preservable files |
| 2 | nothing-to-merge | [`_integrate_empty_or_already_merged`](../../hermes_cli/kanban_worktrees.py) | handles `ahead == 0`: already-integrated, or replays a previously reverted merge |
| 3 | dirty overlap | inline in `integrate_chain` | a foreign dirty file in the live checkout overlaps the branch diff |
| 4 | rebase onto target | [`_integrate_rebase_branch`](../../hermes_cli/kanban_worktrees.py) | conflict → `rebase_conflict` (routed back to the coder, **not** a park) |
| 5 | merge + post-merge gate | [`_integrate_merge_and_gate`](../../hermes_cli/kanban_worktrees.py) | merge conflict → `merge --abort`; red gate → `revert -m 1` + park |

The post-merge gate runs at the exact merge commit inside a detached validation
worktree — [`_run_gate_in_validation_worktree`](../../hermes_cli/kanban_worktrees.py)
— never in the possibly-dirty live checkout. Every exception path there returns
`(False, …)` — it fails **closed**.

Gate selection is
[`_integration_gate_for_repo`](../../hermes_cli/kanban_worktrees.py):
a per-repo command list from `kanban.integration_gate.repos` if configured, else
[`fo_integration_gate`](../../hermes_cli/kanban_worktrees.py) for the FO
repo, else [`default_quick_gate`](../../hermes_cli/kanban_worktrees.py).
**This repo is not in `integration_gate.repos`, so it uses `default_quick_gate`.**

`default_quick_gate` = ruff over the changed `.py` files, then the *affected*
pytest modules, then `lint:control` + `tsc -b` + control Vitest when the diff
touches `web/`. `npm run build` is deliberately excluded (it mutates generated
dashboard assets and belongs to the release gate).

Outcomes: `merged` / `clean` (nothing to merge) / `parked` / `rebase_conflict`.
A park becomes [`_park_integration`](../../hermes_cli/kanban_db.py),
which blocks the task and stamps the closing run `integration_parked` rather
than `completed` — a parked integration must not count as a success for the
respawn guard or per-profile stats.

### Chain semantics: only the last task merges

[`maybe_integrate_on_complete`](../../hermes_cli/kanban_worktrees.py) only
integrates when this completion closes the **last open task** of a provisioned
chain. [`_find_open_chain_sibling`](../../hermes_cli/kanban_worktrees.py)
ORs two signals conservatively: `task_links` membership from the chain root, and
any task whose `workspace_path` lives under the same worktree. Consequence: an
ordinary mid-chain slice goes `done` with **nothing merged**, and the merge
event lands on a *different* task ID than the one that wrote the code. Do not
read "task X is done" as "X's code is on main".

## Review tiers and the four token levers

The live `~/.hermes/config.yaml` encodes four deliberate cost decisions. They are
policy, not accident — flipping one buys quality and costs tokens, in the
direction stated here.

```yaml
kanban.review_gate:
  auto_tier: true                   # tier chosen mechanically, not by hand
  standard_uses_llm_verifier: false # lever 1
  judge_at_chain_tip: true          # lever 2
  critical_reviews_each_slice: false# lever 3
  code_roles: [coder, coder-frontend, premium] # lever 4 (scope of the whole gate)
```

**Tier resolution** —
[`_effective_review_tier`](../../hermes_cli/kanban_db.py) treats the
deterministic classifier as a *floor*: an operator may always RAISE the tier,
but a downgrade below the floor only applies with a logged
`review_tier_downgrade_ack` event, checked by
[`_review_tier_downgrade_acked`](../../hermes_cli/kanban_db.py). The
classifier itself,
[`classify_review_tier`](../../hermes_cli/control_plane_gate.py), reads
**only prose** — `risk_class`, the title, and the body. It never sees the diff.

**Lever 1 — `standard_uses_llm_verifier: false`.** A standard-tier completion
skips the LLM verifier when the deterministic worker gate ran green. The skip
requires *positive* evidence: [`_run_worker_gate`](../../hermes_cli/kanban_db.py)
returns `{"configured": False}` (no `passed` key) whenever the gate is disabled,
the assignee is not code-bearing, the workspace is missing, or no commands match
the repo — and `stamp.get("passed") is True` is then False, so the task parks in
review. Disabled/misconfigured therefore fails **safe**. A green process exit is
also insufficient: `_worker_gate_has_positive_test_count`
requires a recognized runner summary with more than zero passed tests before
review may be skipped or deferred. Zero-test and unknown-count gates therefore
park for review instead of passing vacuously.

**Lever 2 — `judge_at_chain_tip: true`.** A non-tip `review`-tier slice defers
its LLM review to the chain tip, so one judgment covers the feature instead of
one per slice. Cost: the tip's review diff is captured by
[`_capture_review_diff_snapshot`](../../hermes_cli/kanban_db.py) against
that run's own `pre_run_commit_sha`. In a shared chain worktree the earlier
slices are already in that baseline, so a mid-chain regression is **outside the
diff the tip judge sees**. The against-main baseline exists but is only used for
stage>0 re-submissions. The only thing that sees the accumulated diff is the
mechanical post-merge gate.

**Lever 3 — `critical_reviews_each_slice: false`.** Lets `critical`-tier slices
also defer to the tip. Same trade as lever 2, applied to the highest tier.

**Lever 4 — `code_roles: [coder, coder-frontend, premium]`.** The review gate
is scoped by *assignee name*, not by whether the diff contains code. Both the
backend `coder` lane and the Control-SPA `coder-frontend` lane are therefore
reviewed; a code-bearing task assigned to any other profile is never reviewed
and never tier-classified. It still merges through the integrator with only
the post-merge gate.

Note `_review_gate_config` deliberately reads the **root** config: a worker's
own profile config must not be able to disable the gate it is subject to.

## Stall and failure modes

| mode | trigger | enforcing code | confirm live | clear |
|---|---|---|---|---|
| Dispatch tick lock | another process owns the board lock, or lock setup cannot prove ownership | [`_dispatch_tick_lock`](../../hermes_cli/kanban_db.py) | tick result has `skipped_locked=True`; no maintenance/spawn writes from that tick | stop the duplicate dispatcher or repair lock-path access; next tick retries automatically |
| Respawn cooldown/duplicate-work guard | recent rate limit/transient retry/auth failure/success, active PR, or invalid code contract | [`check_respawn_guard`](../../hermes_cli/kanban_db.py) | card remains `ready`; deduped `respawn_guarded` event names the reason | wait for cooldown, correct auth/contract, close or deliberately requeue after successful work, resolve the PR |
| G1 cumulative input-token runaway | all-run input sum exceeds configured per-task cap; one actionable review extension may be allowed | [`_park_budget_runaway`](../../hermes_cli/kanban_db.py) | card becomes capacity-blocked; `budget_runaway_parked` and operator-escalation events include sum/cap/run count | inspect the runaway, then operator unblocks/reassigns/closes; raising the cap alone does not change the parked state |
| Dispatch holds | repo, chain-worktree, writer lease, daily budget, role mismatch, or per-profile concurrency is occupied | [`summarize_dispatch_holds`](../../hermes_cli/kanban_db.py) | card stays `ready`; tick bucket/event names `repo_serialized`, `chain_worktree_serialized`, `worktree_writer_active`, `budget_held`, `role_fit_held`, or profile cap | let holder finish/reclaim, repair stale writer ownership, adjust routing/cap, or remove the conflicting work |
| Review ping-pong breaker | repeated review-origin `REQUEST_CHANGES` reaches configured maximum rounds | [`block_task`](../../hermes_cli/kanban_db.py) | task is `blocked` as `needs_input`; reason starts `review ping-pong breaker`; operator escalation class is `review_pingpong` | operator resolves findings and explicitly unblocks/respecs/reassigns; it does not auto-retry |
| Parent/wait gate | parent is not `done`, due time is future, or typed wait remains unsatisfied/invalid | [`recompute_ready`](../../hermes_cli/kanban_db.py) | task remains `todo`/`blocked`; claim may emit `claim_rejected` | finish parent, wait for due/event, or use the audited wait override |
| Global/profile concurrency | live running count reaches board, spawn, or profile cap | [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py) | no spawn; profile-capped tasks appear in the hold result, global cap returns early | allow running tasks to terminate/reclaim or change the configured cap |
| Nonspawnable assignee | assigned name is not an on-disk Hermes profile | [`_dispatch_once_locked`](../../hermes_cli/kanban_db.py) | `nonspawnable` event; unknown lanes also emit one operator escalation | assign a real profile or provision the intended profile; terminal pull-only lanes remain intentionally held |
| Workspace provisioning/base drift | invalid path, worktree lock/timeout, rebase conflict, or reused base differs | [`_resolve_dispatch_workspace`](../../hermes_cli/kanban_db.py) | claimed run is requeued/blocked; events include spawn retry/failure or `worker_base_rejected` | fix path/git state; transient timeouts back off; conflicts use the bounded fixer path or operator repair |
| Claim/heartbeat expiry | TTL expires and no sufficiently fresh observable progress exists | [`release_stale_claims`](../../hermes_cli/kanban_db.py) | `reclaimed`, `claim_extended`, or `reclaim_deferred` event; status is `ready`, `review`, or still `running` | healthy workers heartbeat; terminate/reclaim a wedged process; never clear ownership while its process survives |
| Worker exits without terminal lifecycle call | subprocess exits zero while task is still running | [`detect_crashed_workers`](../../hermes_cli/kanban_db.py) | `protocol_violation` or `deliverable_posted_not_completed`; bounded repeats end in `gave_up` | recover posted evidence or rerun with the required complete/block call; operator unblocks after breaker trip |
| Spawn/config failure breaker | model route, executable, workspace, or repeated spawn fails | [`_record_spawn_failure`](../../hermes_cli/kanban_db.py) | failure counter and spawn events accumulate; terminal attempt becomes blocked/auto-blocked | repair deterministic config; allow bounded transient retry, then explicitly unblock after correction |
| Sticky worker/operator block | latest block or active escalation requires a decision | [`_has_sticky_block`](../../hermes_cli/kanban_db.py) | card stays `blocked` even when every parent is done | [`unblock_task`](../../hermes_cli/kanban_db.py) after resolving the stated cause |
| Integration park | a precheck, the merge, or the post-merge gate failed | [`integrate_chain`](../../hermes_cli/kanban_worktrees.py) → [`_park_integration`](../../hermes_cli/kanban_db.py) | `integration_parked` event; closing run outcome is `integration_parked`, task `blocked` | fix the stated cause, then unblock; a red gate means the merge was already reverted |
| Rebase conflict | chain branch does not replay onto the live target | [`_integrate_rebase_branch`](../../hermes_cli/kanban_worktrees.py) | `integration_rebase_conflict`; rebase aborted, worktree back at its committed state | routed back to the coder as fixer work, deliberately NOT an operator park |
| Integration retry exhausted | a `transient`-classed park kept failing | [`_integration_park_class`](../../hermes_cli/kanban_worktrees.py) | bounded `integration_retry` events; re-park once reclassified non-transient | resolve the underlying dirt/lock; the retry counter is separate from the failure breaker |

## Traps

Each of these has cost a real session real time. They are invisible from the
state diagram.

**`HERMES_HOME` does not isolate the board.** Board paths resolve through
[`kanban_home`](../../hermes_cli/kanban_db.py) /
[`kanban_db_path`](../../hermes_cli/kanban_db.py), which anchor to the
shared hermes root on purpose — otherwise every profile would silently fork its
own board and break the dispatcher/worker handoff. So a test or probe that sets
only `HERMES_HOME` still reads and **migrates the live `kanban.db`**. Set
`HERMES_KANBAN_HOME` as well for a genuinely isolated run. This has leaked
production rows at least twice.

**The route is frozen at claim.** See the route section above. Anything reading
the mutable task row for routing at spawn time is dead code.

**`connect()`'s fast path skips the integrity guard.** Once a board carries the
current schema stamp, [`_try_fast_connect`](../../hermes_cli/kanban_db.py)
compares `PRAGMA user_version` and returns;
[`_guard_existing_db_is_healthy`](../../hermes_cli/kanban_db.py) runs only
on the cold init path in [`connect`](../../hermes_cli/kanban_db.py) or when
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
[`_record_task_failure`](../../hermes_cli/kanban_db.py);
[`_record_spawn_failure`](../../hermes_cli/kanban_db.py) delegates to it.
Grep finds the old name first, so a change applied only there misses the crash
and timeout paths that call `_record_task_failure` directly.

**Affected-test selection is fail-closed.** Both the worker gate and the
post-merge gate classify every changed path as `selected`, `not_applicable`,
`allowlisted`, or `unmapped`. `scripts/run-affected.sh` may skip pytest only
when every changed path is explicitly not applicable or allowlisted; an
in-scope production Python path with no test selection exits 4 before pytest.
The post-merge [`default_quick_gate`](../../hermes_cli/kanban_worktrees.py)
enforces the same hold. Its compatibility mapper,
[`_affected_pytest_modules`](../../hermes_cli/kanban_worktrees.py), and
the standalone script both delegate to the shared
[`classify_changed_paths`](../../hermes_cli/affected_test_mapping.py).
The repository census is contract-tested at zero unmapped production paths in
both modes; a synthetic new production path must still become `unmapped`.
The compatibility mappers also throw on `unmapped`; they never collapse that
state into an empty, apparently successful test list. Explicit patterns are
unioned with direct and AST-derived import tests rather than replacing them.
Deleted production paths retain surviving direct/import coverage and otherwise
become `not_applicable`; deletion never silently removes a still-importing test
from the gate.

**The two fallback caps and worker union cap live in one classifier.**
[`WORKER_FALLBACK_MAX_TEST_FILES`](../../hermes_cli/affected_test_mapping.py)
is 200 for the interactive worker gate, while
[`INTEGRATION_FALLBACK_MAX_TEST_FILES`](../../hermes_cli/affected_test_mapping.py)
is 800 for the post-merge integration gate. The selected mode applies its cap
before assigning the path state. An oversized fallback without a focused test
is therefore `unmapped`, not a successful mapping whose test directory is
silently removed afterward. Do not move the cap back into a shell post-filter
or duplicate the mapping tables across consumers.

The interactive worker also caps each focused direct/explicit/import union at
217 files: direct evidence first, then explicit, then import, stable-sorted
inside each tier. This preserves the measured 217-file `kanban_db.py` core case
while keeping observed reserve under the 1200-second worker timeout. An
oversized union stays `selected` and emits a stderr warning naming the path and
selected/discarded counts; turning it into `unmapped` would create a gate
deadlock without an escape. Integration is deliberately uncapped and runs the
full union, while the nightly suite remains the backstop. Thus only the
interactive tempo gate is truncated; the merge gate retains complete additive
evidence.

Stress-registry scenarios are excluded from that pytest import evidence.
Non-`test_*.py` support files under `tests/` are intentionally
`not_applicable` in both modes. This accepted boundary means a support-only
diff can skip pytest; the rejected directory fallback had no legal escape,
classified the two modes differently, and falsely selected test-free
directories. The nightly full suite is its backstop; see ADR 0003.

Both the worker and post-merge integrator obtain commit-diff paths from the
same classifier helper, including deletions and typechanges. Its git subprocesses
honor `HERMES_WORKTREE_GIT_TIMEOUT`; under the serialized integrator a timeout
parks as transient instead of becoming a mapping error or holding the lock
indefinitely. Repository census and import inventory use tracked production
files only, so unrelated untracked slice work cannot turn the global census red.
Existing untracked tests may still supply path-local evidence while a slice is
being built.

An active audited exception is evaluated before the package fallback so its
meaning does not change between worker and integration mode. Explicit, direct,
or import-index coverage still conflicts with an active exception. Expired or
stale entries are ignored with a path-local warning and degrade only their
affected path to `unmapped`; malformed or duplicate inventory remains a mapping
error.

**A park cannot demote a task that is already `done`.**
[`_park_integration`](../../hermes_cli/kanban_db.py) blocks via an UPDATE
constrained to `status IN ('running','ready','blocked')` and returns `False`
when `rowcount != 1` — silently, with the `integration_parked` event already
written. When the integrator runs after the row reached `done`, the event
appears but the status does not change. Measured 2026-07-25 on the live board:
106 `integration_parked` events but only 50 runs stamped with the
`integration_parked` outcome, and four `done` tasks whose closing run is stamped
`completed` while carrying a red-gate park reason (`t_81a35a60` pytest exit -15,
`t_461aee5e` pytest exit 1, `t_77ffe9cc`, `t_daed5e85`). Their work was reverted
by the gate and never landed, yet they read as successfully done. When auditing,
trust `task_runs.outcome`, not `tasks.status`.

**This file's anchors are mechanically checked.**
`scripts/check_kanban_lifecycle_anchors.py` resolves top-level symbols through
the module AST and banner titles by exact line comparison;
`tests/scripts/test_check_kanban_lifecycle_anchors.py` enforces it, and
`scripts/affected_tests.py` maps both the target modules and this document to
that test.

## Other entry points into the lifecycle

The transition table starts at
[`create_task`](../../hermes_cli/kanban_db.py) because that is the common
path, but it is not the only one. Tasks also enter the board through epic/triage
fan-out (`hermes_cli/kanban_decompose.py`), PlanSpec ingest
(`hermes_cli/planspecs.py`, plus `hermes_cli/pa_planspec.py` for drafts), and
`create_epic`, which maintains a parallel `open`/closed object referenced by
`tasks.epic_id`. All of them funnel into the same state vocabulary, so the
transition table still applies once the row exists.

Structured acceptance criteria may carry a `route`, which is preserved in the
task's `acceptance_criteria` payload and rendered as the concrete acceptance
location in reviewer and chain-tip briefs. Scope follows a different path:
PlanSpec intake writes the human-readable `## Scope Contract` body block but
does not populate `tasks.scope_contract`. Strategist-generated code work must
therefore derive concrete `scope_files` from grounding or insert a scout
dependency before the ungrounded build slice.

## Section index

| banner title |
|---|
| [Constants](../../hermes_cli/kanban_db.py) |
| [Paths](../../hermes_cli/kanban_db.py) |
| [Data classes](../../hermes_cli/kanban_db.py) |
| [Vault / Memory link extraction](../../hermes_cli/kanban_db.py) |
| [Schema](../../hermes_cli/kanban_db.py) |
| [Connection helpers](../../hermes_cli/kanban_db.py) |
| [ID generation](../../hermes_cli/kanban_db.py) |
| [Task creation / mutation](../../hermes_cli/kanban_db.py) |
| [Links](../../hermes_cli/kanban_db.py) |
| [Comments & events](../../hermes_cli/kanban_db.py) |
| [Attachments](../../hermes_cli/kanban_db.py) |
| [Dependency resolution (todo -> ready)](../../hermes_cli/kanban_db.py) |
| [Claim / complete / block](../../hermes_cli/kanban_db.py) |
| [Review gate (Phase 2: independent verification before 'done')](../../hermes_cli/kanban_db.py) |
| [Workspace / tmux cleanup](../../hermes_cli/kanban_db.py) |
| [First-use tip for scratch workspaces](../../hermes_cli/kanban_db.py) |
| [Workspace resolution](../../hermes_cli/kanban_db.py) |
| [Dispatcher (one-shot pass)](../../hermes_cli/kanban_db.py) |
| [Respawn guard constants](../../hermes_cli/kanban_db.py) |
| [G1: per-task cumulative input-token runaway guard](../../hermes_cli/kanban_db.py) |
| [OpenClaw cross-system dispatch (Mission-Control via HMAC-signed envelopes)](../../hermes_cli/kanban_db.py) |
| [B1a — tree-wide inventory/hygiene must not land on research/premium LLM loops](../../hermes_cli/kanban_db.py) |
| [Long-lived dispatcher daemon](../../hermes_cli/kanban_db.py) |
| [Worker context builder (what a spawned worker sees)](../../hermes_cli/kanban_db.py) |
| [Scope-contract template expansion (PlanSpec B)](../../hermes_cli/kanban_db.py) |
| [Stats + SLA helpers](../../hermes_cli/kanban_db.py) |
| [Epics (N-E3) — durable goals spanning multiple task trees](../../hermes_cli/kanban_db.py) |
| [Disposition Ledger (FRD-S1) — additive; no wiring into completion path yet](../../hermes_cli/kanban_db.py) |
| [Lanes (night-sprint F1) — switchable profile→(runtime, model) presets](../../hermes_cli/kanban_db.py) |
| [Notification subscriptions (used by the gateway kanban-notifier)](../../hermes_cli/kanban_db.py) |
| [Browser Web Push subscriptions (used by the control dashboard)](../../hermes_cli/kanban_db.py) |
| [Retention + garbage collection](../../hermes_cli/kanban_db.py) |
| [Worker log accessor](../../hermes_cli/kanban_db.py) |
| [Assignee enumeration (known profiles + per-profile board stats)](../../hermes_cli/kanban_db.py) |
| [Runs (attempt history on a task)](../../hermes_cli/kanban_db.py) |
| [Durable Kanban ↔ TMAX execution capsule](../../hermes_cli/kanban_db.py) |
