# Reliability Landing Report

**Branch:** `reliability/upstream-audit-salvage` (worktree `/home/piet/.hermes/worktrees/reliability-land`)
**Date:** 2026-07-25
**Status:** all 3 steps landed, Codex-green, gates green. **Not merged, not pushed** — the final gate is the operator's, per brief.

| Step | Outcome | Codex verdict | Commit |
|---|---|---|---|
| 1 — delivery-ledger reply anchor | **Landed** | PASS after 1 blocker + 1 follow-up | `c6d0f264d` |
| 2 — credential fingerprint routing | **Landed** | PASS after 2 blockers + 1 follow-up | `b897f107a` |
| 3 — ws send-drain queue | **Landed** (race claim verified real) | PASS after revert-recommendation reversed | `e1787ecc0` |

---

## Brief bug found up front (important)

**`1-delivery-ledger-reply_to-claim_owned_retry.patch` is mislabeled.** It contains commit
`2e6af5a22` *"codex: preserve gateway delivery invariants"* — post-stream media delivery,
dashboard `web_dist` validation, `tui_gateway/server.py`. It touches **`gateway/run.py` but
never `gateway/delivery_ledger.py`**, and contains no `reply_to`, `metadata_json`, or
`claim_owned_retry`.

The real Step-1 delta is commit **`a2096699d`** *"codex: close qwen wave B review findings"*
on branch `codex/upstream-audit-20260722`. I sourced Step 1 from there instead. Two notes:

- `ec59ec8c4` (the ledger's base module) **has already landed on main** — only the Wave-B
  delta was outstanding.
- `a2096699d` also carries an unrelated turn-lease rotation callback. That is **outside the
  brief's Step-1 scope and was deliberately not ported.**

## Divergence trap avoided

The salvage delta replaces `apply_wal_with_fallback(...)` with a raw
`PRAGMA journal_mode=WAL`. That is main's *newer* code being reverted by the older base —
**not ported.** `apply_wal_with_fallback` is retained.

---

## Step 1 — delivery-ledger reply anchor (`c6d0f264d`)

**Bug:** crash-recovered redelivery rebuilt metadata as `{"thread_id": ...}` and passed no
reply anchor at all, so a threaded reply came back detached from its conversation.

**Diff summary** (5 files, +330/−38):
- `gateway/delivery_ledger.py` — nullable `reply_to` + `metadata_json` columns; additive
  in-place `ALTER` migration guarded by `PRAGMA table_info` (legacy rows degrade to exactly
  the previous thread_id-only metadata); threading through `record_obligation()` /
  `sweep_recoverable()`; new `claim_owned_retry()` so recovery spends its remaining durable
  attempt budget in-process.
- `gateway/platforms/base.py` — producer records the same `_reply_anchor` /
  `_final_thread_metadata` it hands to the real send.
- `gateway/run.py` — redelivery replays the full routing contract and retries in-process.

**Red→green evidence:** with the new ledger but the *old* `run.py`, exactly 4 behaviour
tests fail with `KeyError: 'reply_to'` — the anchor is genuinely lost. (A blunter probe
reverting all source produced 30 reds, but most were helper-signature breakage; the
isolated probe is the real proof.)

**Codex round 1 — BLOCK:** leaving `resume_pending` armed on exhaustion lets the resume path
emit a second, unmarked answer.
**Round 2 — BLOCK sustained:** my ambiguous/definitive split was insufficient, because Slack
posts a long reply chunk-by-chunk (`plugins/platforms/slack/adapter.py:2541`) and returns
`success=False` at line 2616 *after* earlier chunks landed. I verified this in the source.
**Resolution:** dropped the `resume_pending` semantics change entirely — it was the risky
rider and outside Step 1's stated scope. `resume_pending` clearing is now byte-identical to
pre-diff main; the durable attempts cap bounds an undeliverable row.
**Round 3 — PASS**, no remaining blockers.

**Gates:** `ruff` clean on all 3 modules. `test_delivery_ledger` 34 passed,
`test_delivery_ledger_producer` 9, `test_restart_redelivery_dedup` 13, `test_delivery` 33,
`test_discord_missed_message_backfill` 44.

---

## Step 2 — credential fingerprint routing (`b897f107a`)

**State of main first:** main had already evolved past the salvage base — it has
`credential_id` threading and `try_refresh_matching`, plus `identity_supplied` guards. The
salvage's `credential_id_hint` / `try_refresh_entry` naming was **not** reintroduced as a
duplicate. What was genuinely still missing:

1. the **HMAC ownership trail across rotation** (main matched the *current* key only), and
2. the **xAI proxy adapter**, which recovered with `try_refresh_current()` /
   `mark_exhausted_and_rotate(status_code=...)` — **no identity hint at all**, so it always
   acted on the shared cursor.

**Red-first:** the brief's named test `test_late_oauth_failure_matches_recently_rotated_token`
plus 4 more went red (`AttributeError: no matching_entry`); the 3 xAI tests went red
(no `credential_id`; the wrong account's refresh token spent).

**Diff summary** (5 files, +576/−15): keyed-HMAC fingerprint trail
(`os.urandom(32)` secret per pool, so no historical bearer is retained), seeded in
`__init__`, extended on `_replace_entry` and `add_entry`, bounded to
`_RUNTIME_KEY_GENERATIONS = 4` per credential; `UpstreamCredential.credential_id`; xAI
adapter threads the id, falling back to the bearer only when no id is known.

**Codex round 1 — BLOCK (2):**
- *Duplicate fingerprints overwrite the prior owner* — the old linear scan returned the
  **first** match and `mark_exhausted_and_rotate`'s sibling-marking depends on one key
  backing several entries. **Fixed:** `_matching_entry_unlocked` now does the original
  current-key scan **first** (first-wins, semantics byte-identical) and only falls through
  to the trail when the key is current for *nobody* — purely the rotated-key case.
- *Removal never purges the maps.* **Fixed:** `_forget_runtime_key_owner()` +
  `_prune_runtime_key_owners()`, called from `_persist()` — the chokepoint every removal
  path funnels through.

**Round 2 — B1 closed, B2 not:** `remove_index()` bypasses `_persist()` and writes through
directly. Verified in source; **fixed** by pruning there too. I audited all three
`write_credential_pool` call sites; Codex confirmed the audit (the third is in `load_pool`,
which builds a fresh pool whose `__init__` reseeds).
**Round 3 — PASS**, no new defects.

**Gates:** `ruff` clean. `test_credential_pool` 105 passed, `test_credential_pool_routing`
28 passed, `test_proxy` 42, `test_auth_commands` 53, `test_codex_xai_oauth_recovery` 51,
plus ~2400 further credential-related tests across ~70 files.

---

## Step 3 — ws send-drain queue (`e1787ecc0`)

The brief required verifying the race claim before adopting. **The claim is real.** Ported
the ordering test against unmodified main:

```
assert sent == ['TOKEN', '{"kind": "CONTROL"}']
E  assert ['{"kind": "CONTROL"}', 'TOKEN'] == ['TOKEN', '{"kind": "CONTROL"}']
```

Tokens landed *after* the frame meant to follow them. Root cause: `asyncio.Lock` is acquired
in **task-scheduling order, not enqueue order**, so an already-ready control frame overtakes
a timer-fixed token batch. Current code still had the exact pre-diff structure
(`_send_lock` + `create_task(_safe_send_many(...))`).

**Diff summary** (2 files, +251/−36): FIFO `deque` + a single loop-owned drain task as the
sole socket writer; each batch carries a completion future, which is what the thread-side
`write()` still blocks on (its "loop stalled, do NOT latch closed" timeout semantics are
unchanged). `_safe_send_many` kept as a shim — no production caller outside `ws.py`.

**Codex round 1 — BLOCK + "RECOMMEND_REVERT: yes":** `CancelledError` bypasses
`except Exception`, killing the sole writer and stranding queued batches forever
(`write_async` never completes). **Reproduced it** with a test that hung on `TimeoutError`,
then fixed: release all waiters on cancellation without respawning a drain that teardown
would only cancel again, plus a `finally` that hands the remainder to a fresh drain on any
unexpected exit.

**Round 2 — blocker closed but 2 more raised:**
- *`call_soon_threadsafe` failure released only the newest batch.* Accepted and **fixed** —
  the whole queue is now released. (Needed a lock-held variant: `_queue_send_locked` already
  holds the non-reentrant `_token_lock`, so calling the locking helper would self-deadlock.)
- *A `send_text` that never returns hangs the awaiting caller.* I **refuted this as a
  regression**: pre-diff `write_async` awaited `_safe_send_many`, which awaited `send_text`
  directly with `_closed` checked only *between* lines — identical behaviour. Codex
  confirmed (`D1_IS_PREEXISTING: yes`, citing `HEAD:tui_gateway/ws.py:225-237`).

**Round 3 — `RECOMMEND_REVERT: no`, `NEW_DEFECTS: none`.** Step 3 landed rather than dropped.

**Gates:** `ruff` clean. `test_tui_gateway_ws` 11 passed, `test_tui_gateway_server` 442
passed, plus ~70 further `tui_gateway` test files green.

---

## Verification discipline

Every fix in this branch has a regression test, and **every regression test was control-probed**:
reverted the specific fix and confirmed the test fails. That covers the ambiguity guard, the
shared-key first-wins scan, the `_persist` prune, the `remove_index` prune, the drain
cancellation handler, and the queue-failure release.

## Pre-existing reds (NOT caused by this branch)

Each verified identical on a clean tree at this branch point:

| Test | Note |
|---|---|
| `test_credential_pool_routing::test_auth_refresh_uses_stable_id_after_runtime_key_changes` | Codex: exercises the existing stable-id path; out of Step 2's anti-scope |
| `test_credential_pool_routing::test_unmatched_key_does_not_retry_only_pool_entry` | Codex: deliberate unknown-key fail-closed behaviour |
| `test_model_catalog::test_in_repo_lists_match_manifest` | stale generated manifest — `python scripts/build_model_catalog.py` |
| `tools/test_mcp_tool.py` (1) | unrelated |
| `tui_gateway/test_entry_dispatch_guard.py` (1) | unrelated |
| `tui_gateway/test_protocol.py` (93 errors) | unrelated |

## Closing sweep

`pytest --co -q tests/` → **exit 0**, 54406/54469 collected (63 deselected).

## Merge notes for the operator

- `main` advanced by one parallel commit (`304989592`, kanban provider_override) while this
  work ran. My branch is based on `1b1049c47`.
- **Zero file overlap** with that commit, and `git merge-tree` reports **0 conflict markers**.
  (The comparison was control-probed against a known-overlapping input.)
- Nothing was merged or pushed. `origin` = NousResearch — never push there.
- The three commits are independent and can be taken separately if desired.
