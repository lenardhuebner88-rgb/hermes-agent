# Brief — Qwen session 4: claude_effort UI wiring + mobile-first operative pass, then land live

**From:** Claude Code (orchestrator) · **To:** fresh interactive qwen3.8-max-preview session (tmux `work:qwen38-effort`)
**Operator directive (Piet, 2026-07-24 ≈13:30):** (S1) wire `claude_effort` into the /lanes UI as the promised follow-up slice, and (S2) make the /lanes UI overall more modern and *operative*, with mobile as the primary focus. Same standing mandate as relays 2/3: after green verification you land it live yourself per §L.

## Where you are

- Worktree `~/.hermes/hermes-agent/.claude/worktrees/qwen-lanes-mobile`, branch `qwen/lanes-effort-mobile`, base `f41209933` (relay-3 state). Web deps pre-installed.
- Read first: `docs/handoff/2026-07-24-qwen-lanes-polish-REPORT.md` §W3 (the claude_effort investigation — CLI evidence, code sites) and §W5 (save-bar fix). Cage rules from `docs/handoff/2026-07-24-qwen-lanes-model-switch-verify.md` §1 bind you (Abo-only real calls, no secrets in logs, never origin/--force, live checkout untouched before §L, other worktrees/windows untouched).
- **Design language is BINDING:** `web/src/control/DESIGN.md` + tokens in `web/src/control/theme.css`. The token ratchet in `scripts/gate-frontend.sh` will fail raw-hex/off-token styling. Mobile breakpoint context: `tab` = 600px, matrix collapses below `min-[52rem]`.

## S1 — claude_effort UI wiring (design decisions PRE-MADE — implement, don't re-litigate)

Relay 3 proved: `claude --effort low|medium|high|xhigh|max` is real, Hermes maps profile field `claude_effort` → `--effort` at worker spawn (`kanban_db.py` ~30560), and the lanes control's `agent.reasoning_effort` is a no-op for claude-cli (which is why claude-cli rows now show no-Knopf + hint).

1. **Support set:** claude-cli rows get `reasoning_support = [low, medium, high, xhigh, max]` — the full transport truth, not the hermes trio. `reasoning_support_for` for hermes rows stays untouched.
2. **Persist field per runtime:** on persist, claude-cli rows write **`claude_effort`** (top-level profile-config field, exactly where the spawn mapping reads it) and never `agent.reasoning_effort`; hermes rows keep writing `agent.reasoning_effort` byte-identically. Read-back: `_scan_lane_profiles` reports the current `claude_effort` as the selected level for claude-cli rows (unset → STD).
3. **Locked-row semantics — the one deliberate change:** claude-cli rows stay locked for model/fallback/probe (unchanged reasons), but the Reasoning segment becomes **selectively active**. Remove the relay-3 "hier nicht schaltbar" hint on these rows (it is now false); keep the row's locked reason for the other controls. This selective unlock is operator-approved here — do not generalize it to any other locked-row type.
4. **Segment labels:** reuse the existing joined segment strip (STD·LOW·MED·HIGH + XHI·MAX as needed) — extend `ReasoningControl` for a 5-level set without breaking the hermes 3-level rendering or its tests. Keep the WCAG ≥24px target size.
5. **Evidence chain (all three, in the report):** (a) regression tests: backend persist-writes-`claude_effort`-not-`agent.reasoning_effort` + read-back, frontend segment rendering for the 5-level set; (b) a unit-level proof against the REAL spawn-mapping code path that a persisted `claude_effort` lands in the constructed `--effort` argument; (c) live after §L: set an effort on one claude-cli profile via the API, show the profile config now carries `claude_effort`, then **revert it to the prior state** (leave live config as you found it).

## S2 — mobile-first operative pass (bounded modernization, NOT a redesign)

Goal: on a phone (390×844) /lanes should feel like an operator tool — everything reachable, nothing cramped, no horizontal scrolling. Reproduce the current mobile state with render screenshots FIRST (per view section), then improve. Priorities in order:

1. **ProfileMatrix on <52rem:** today the 6-column grid collapses awkwardly. Give each profile a clean stacked card: profile name + model select full-width, reasoning strip + fallback + probe in a compact action row beneath. No information loss, no horizontal scroll.
2. **Touch targets:** every interactive element ≥44px on mobile (`min-h-11` pattern already used in ModelSelect — apply consistently; desktop keeps its denser sizes via `min-[52rem]:`/`tab:` variants).
3. **Operative shortcuts:** lane switching + "Speichern & aktivieren" reachable without long scrolling (e.g. sticky lane bar or jump affordance — pick the lightest mechanism that fits DESIGN.md; no new nav paradigms).
4. **Kompass + SmokePanel on mobile:** cards full-width, meters legible, tap targets per (2). Only layout/spacing — no logic changes.
5. Desktop (≥1280px) must stay visually unchanged — before/after desktop screenshots as regression proof.

Anti-scope: no save-path/serializer logic changes beyond S1's persist branch; no new features (no new panels, no reordering of information architecture); no touching other /control views; no dependency additions. If a priority item turns out to require deeper surgery, ship the lighter items and hand the heavy one back documented.

**Acceptance evidence (paint rule):** component tests cannot see layout — before/after render screenshots at 390px and 1280px for every section you touched, saved to `docs/design/lanes-mockup-renders/` (`effort-mobile-*`), referenced in the report. Keep the lanes E2E (`scripts/lanes-e2e.sh`) green — update its assertions only where behavior legitimately changed.

## Gates (worktree)

`bash scripts/gate-frontend.sh` (exit code is the truth — never pipe) · per-file pytest via `PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest <touched test files> -q` · `ruff check .` · `scripts/lanes-e2e.sh`.

## §L — Land live (operator-granted; follow EXACTLY)

Identical to the previous relays — full script in `docs/handoff/2026-07-24-qwen-lanes-SHIP-brief.md` §4/§5: live-checkout preflight, `git merge --ff-only qwen/lanes-effort-mobile` (ff fails → rebase HERE, re-run ALL gates, retry), merged-state gates (`gate-frontend.sh`, `scripts/run-affected.sh`, `pytest --co -q tests/`, `ruff check .`), `CONFIRMED=1 scripts/deploy_dashboard.sh`, loopback auth smoke (`overall=healthy`), live proof (S1 evidence (c) + one live 390px screenshot of the new mobile layout), fork sync + `rev-parse main piet-fork/main` equal — never origin, never --force. Red gate / unhealthy smoke / preflight STOP → no rollout, blocker into the report, Discord note, end.

## Report & closeout

`docs/handoff/2026-07-24-qwen-lanes-effort-mobile-REPORT.md` committed: S1 evidence chain (a/b/c), S2 per-priority before/after screenshot paths, gate lines verbatim, live proof, rev-parse hashes, anything handed back. Discord via `/home/piet/.hermes/hermes-agent/venv/bin/hermes send -t discord` — ⚠️ guard hook keyword-blocks rollout verbs ("Push"/"deploy") in message text; write "Fork aktualisiert"/"live gebracht".
