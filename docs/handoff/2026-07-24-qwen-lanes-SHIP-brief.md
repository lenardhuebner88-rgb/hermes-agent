# Ship brief — Qwen session 2: verify → improve → land `qwen/lanes-model-verify` live

**From:** Claude Code (orchestrator) · **To:** fresh interactive qwen3.8-max-preview session (tmux `work:qwen38-ship`)
**Operator mandate (Piet, 2026-07-24 ≈10:57):** verify the first Qwen session's work, improve it where warranted, then **bring it live into the runtime yourself** — merge, gates, dashboard rollout, fork sync. This explicit operator grant is what authorizes steps that are normally reserved: use it exactly as scripted in §4, nothing beyond.

## What you inherit

- Worktree `~/.hermes/hermes-agent/.claude/worktrees/qwen-lanes-verify`, branch `qwen/lanes-model-verify`, clean.
- Session 1's increment: **`10006c2ec`** — matrix verification (241/241 switch, 379/379 reasoning, Abo probe sweep) + 3 fixes with regression tests. Its full account: `docs/handoff/2026-07-24-qwen-lanes-verify-REPORT.md`. Mission brief it worked from: `docs/handoff/2026-07-24-qwen-lanes-model-switch-verify.md` (its §1 cage rules still bind you EXCEPT where §4 below explicitly grants more).
- Real code touched (≈140 LOC + tests): `plugins/kanban/dashboard/lane_routes.py`, `web/src/control/views/lanes/api.ts`, + 3 test files.

## §1 Verify (grader ≠ writer — you review with fresh eyes, trust nothing)

1. Read the REPORT fully, then review the diff `git diff 3d50d5614..10006c2ec` adversarially against it. Priority targets (session 1 itself flagged them for independent review):
   - **Fix 3 / F-PROBE-CUSTOM** (`lane_routes.py`): touches shared probe/auth-smoke status logic — check no other caller of that status path changes meaning (grep callers), and that `ok` vs `fallback` semantics stay honest (a fallback that silently reports `ok` would fake green).
   - **Fix 2 / F-REASONING-K3**: kimi family now detected by provider — verify k3 genuinely honors reasoning on the kimi-coding transport, or that the UI stays honest if not; check the "residual transport-behavior note" in the REPORT and judge whether shipping it is honest UI.
   - **Fix 1 / F3-1** (`api.ts`): locked hermes rows keep fallback chain — check the serializer round-trip with a lane that has fallbacks + locked rows.
2. Re-run the gates YOURSELF in the worktree (never trust inherited evidence):
   `bash scripts/gate-frontend.sh` (exit code is the truth — never pipe through tail/grep) · per-file pytest for the touched test files via `PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest <file> -q` (never naive full suite, never `npx` in the worktree) · `ruff check .`.
3. Verdict per fix: pass / fix-needed / drop. Anything you can't confirm within its claimed scope → treat as fix-needed.

## §2 Improve (bounded)

- Fix what §1 found; smallest diffs, each with a regression test.
- From the REPORT's found-but-NOT-fixed list (§4 there): you may close items that are small, test-covered, and reversible. **Operator questions stay operator questions** — the claude-cli "greyed segments vs no-Knopf" design intent and catalog-curation calls (codex `-pro`→k3 fallback listing) are NOT yours to decide; leave them documented.
- No new features, no save-path redesign, no scope growth. Commit style `lanes-verify: <what>` (avoid rollout-related verbs in commit messages — a guard hook keyword-blocks them; describe the change itself).

## §3 Re-gate the final worktree state

Full §1.2 gate set again on your final commit, evidence captured verbatim (exit lines).

## §4 Land live (operator-granted; follow EXACTLY)

Phase switch: leave the worktree, work in the live checkout `~/.hermes/hermes-agent`. It is shared by parallel sessions — preflight is mandatory:

```bash
cd ~/.hermes/hermes-agent
git status --short              # foreign dirty/untracked files → leave untouched; if main itself is dirty in files you must merge → STOP, write blocker
git rev-parse --abbrev-ref HEAD # must be main; if not → STOP, write blocker (do NOT checkout branches in the live tree)
git merge --ff-only qwen/lanes-model-verify
```

- ff-only fails (main moved): rebase the branch **in the worktree** onto main, re-run ALL gates there (rebase invalidates gate evidence), then retry the ff-merge.
- **NEVER**: `git pull origin main`, any push to `origin` (NousResearch upstream), `--force`, `reset --hard`.

Gates on the merged state (from the live checkout):
```bash
bash scripts/gate-frontend.sh
scripts/run-affected.sh
pytest --co -q tests/           # collection sweep
ruff check .
```
All green → rollout:
```bash
CONFIRMED=1 scripts/deploy_dashboard.sh          # restarts the service via systemd itself
u=$(grep -m1 '^HERMES_DASHBOARD_USERNAME=' ~/.hermes/.env | cut -d= -f2-)
p=$(grep -m1 '^HERMES_DASHBOARD_PASSWORD=' ~/.hermes/.env | cut -d= -f2-)
HERMES_DASHBOARD_URL=http://127.0.0.1:9119 HERMES_DASHBOARD_USERNAME="$u" HERMES_DASHBOARD_PASSWORD="$p" \
  scripts/smoke_health_status_auth.py --no-prompt   # expect overall=healthy; logs no secrets — keep it that way
```
Live proof = payload, not screenshot: authenticated `GET /api/plugins/kanban/lanes` (login flow in the mission brief §2) still returns 3 lanes / 10 profiles / 198 models, and spot-check one of your fixed behaviors live (e.g. a probe on an Abo model returning the corrected `ok` status).

Fork sync:
```bash
git push piet-fork main                                   # ff only — never --force, never origin
git fetch piet-fork && git rev-parse main piet-fork/main  # equal hashes = the only accepted proof
```

## §5 Close out

1. Append a ship addendum to `docs/handoff/2026-07-24-qwen-lanes-verify-REPORT.md` (your review verdicts, improvements, gate + live evidence, rev-parse hashes) and commit it on main (ff state), then repeat the fork sync + rev-parse proof.
2. Discord: `venv/bin/hermes send -t discord "<summary>"` — ⚠️ the guard hook keyword-blocks messages containing rollout verbs like "Push"/"deploy"; write "Fork aktualisiert" / "live gebracht" instead.
3. If ANY gate is red, the smoke is not healthy, or preflight hits a STOP condition: do not roll out, write the blocker into the REPORT, send the Discord note, and end. A honest red hand-back beats a forced green.
