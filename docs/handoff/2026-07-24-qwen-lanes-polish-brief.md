# Polish brief — Qwen session 3: operator decisions + mobile save-bar bug, then land live

**From:** Claude Code (orchestrator) · **To:** fresh interactive qwen3.8-max-preview session (tmux `work:qwen38-polish`)
**Operator decisions (Piet, 2026-07-24 ≈11:45)** on the open questions from `docs/handoff/2026-07-24-qwen-lanes-verify-REPORT.md` §4, plus one new mobile bug. Same operator mandate as the previous relay: after green verification you land it live yourself, exactly per §L below.

## Where you are

- Worktree `~/.hermes/hermes-agent/.claude/worktrees/qwen-lanes-polish`, branch `qwen/lanes-polish`, base `19c3b535e` (main incl. the shipped verify relay `94a3f794e`). Web deps are pre-installed.
- The live checkout `~/.hermes/hermes-agent` is shared by parallel sessions — never edit it before §L, never run gates there before §L.
- Cage rules from `docs/handoff/2026-07-24-qwen-lanes-model-switch-verify.md` §1 bind you (Abo-only for any real model call; no secrets in logs; never origin/--force; don't touch other worktrees/windows).

## Work items (all five; smallest diffs, each with a regression test)

**W1 — Remove codex `-pro` models from the catalog offer.** Operator: "rausnehmen, sind nicht erreichbar." `gpt-5.6-sol-pro`/`-terra-pro`/`-luna-pro` are not directly served on the Codex endpoint (probe proves fallback to kimi/k3). They must no longer be offered as selectable/`sinnvoll`/probe-able in /lanes. Curation logic: `plugins/kanban/dashboard/lane_routes.py` (~line 459–481 computes `sinnvoll`). Don't break existing lane configs that still reference them — an already-persisted override must keep rendering (locked/warned), only the OFFER disappears.

**W2 — Remove image models from probe scope and offer.** Operator: "ja rausnehmen." Alibaba image/video models (`qwen-image-2.0`, `qwen-image-2.0-pro`, `wan2.7-image`, `wan2.7-image-pro`, …) can't echo a chat token → permanent `fallback` noise. Exclude them from chat-probe/catalog-probe scope AND from the sinnvoll/selectable offer (a chat-lane platform has no use for them). Detect by model class/capability if the catalog exposes one, else by an explicit documented id-pattern list — no fragile substring guessing without a comment.

**W3 — claude-cli reasoning: investigate, then implement or honestly disable.** Operator: "prüfen ob es möglich ist, für claude (Max-Abo, `-p`/CLI-Transport) Reasoning einstellen zu können."
1. Find how Hermes invokes the claude CLI runtime (grep the claude-cli runtime/transport in `hermes_cli`/`agent`). Check what the installed `claude` CLI actually supports for reasoning/thinking in headless `-p` mode: `claude --help`, env vars (e.g. `MAX_THINKING_TOKENS`), model-suffix syntax. Evidence = actual CLI output, not memory.
2. **If configurable:** wire a real reasoning control for claude-cli rows end-to-end (backend `reasoning_support_for` → transport actually applies it → UI segments active). Must include one live proof: a claude-cli call on the Max Abo demonstrating the setting changes behavior/config (cheap single call; claude-cli IS Abo, allowed).
3. **If not configurable:** implement the honest state instead — claude-cli rows show "Modell hat keinen Reasoning-Knopf" (or a "claude-cli transportiert kein Reasoning"-hint), NOT greyed segments (`ReasoningControl` currently only shows no-Knopf for `support=[]`). Document the CLI evidence in the report either way.

**W4 — extra_models for all providers, openrouter-optimal.** Operator: "optimal für openrouter einstellen" → generalize the mechanism so it works for every provider the way it already works for openrouter: `extra_models` from config land in the dropdown catalog (today only `get_configured_provider_extra_models("openrouter")` feeds the catalog — `lane_routes.py` ~140/989 — while other providers' extra_models are merely `admitted`). Keep openrouter behavior byte-identical; add the other providers additively. Real-data test: `alibaba-token-plan` extra_models from `~/.hermes/config.yaml` must appear in the catalog payload.

**W5 — Mobile bug: save/discard bar renders broken at the bottom.** Operator (phone): "Unten ist das Speichern und Verwerfen komisch dargestellt." The save bar (strings: `Speichern`/`Verwerfen`, `web/src/control/views/lanes/strings.ts:41-42`; rendered from `LanesView.tsx`/lanes components) looks wrong on a narrow phone viewport. Reproduce FIRST at mobile width (≈390×844) with a real render screenshot — component tests cannot see paint (proven this week by the pd-N bug). Diagnose (overlap? wrapping? sticky positioning? safe-area? off-screen?), fix, then prove with before/after screenshots at 390px AND ≥1280px (no desktop regression). Screenshots → `docs/design/lanes-mockup-renders/` + referenced in the report.

## Gates (worktree)

`bash scripts/gate-frontend.sh` (exit code is the truth, never pipe through tail/grep) · per-file pytest via `PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest <touched test files> -q` (never naive full suite, never `npx` here) · `ruff check .`.

## §L — Land live (operator-granted; follow EXACTLY)

Identical script to the previous relay — full text in `docs/handoff/2026-07-24-qwen-lanes-SHIP-brief.md` §4/§5 (this repo): live-checkout preflight (`git status --short`, branch must be main, foreign dirty files untouched), `git merge --ff-only qwen/lanes-polish` (ff fails → rebase in THIS worktree, re-run ALL gates, retry), merged-state gates (`gate-frontend.sh`, `scripts/run-affected.sh`, `pytest --co -q tests/`, `ruff check .`), then `CONFIRMED=1 scripts/deploy_dashboard.sh`, loopback auth smoke (creds from `~/.hermes/.env`, expect `overall=healthy`), live payload proof (authenticated `/api/plugins/kanban/lanes`: `-pro` and image models no longer offered, extra_models visible, claude-cli reasoning state per W3), fork sync `git push piet-fork main` + `git fetch piet-fork && git rev-parse main piet-fork/main` equal — never origin, never --force. Any red gate / unhealthy smoke / preflight STOP → do not roll out, write the blocker into the report, Discord note, end.

## Report & closeout

`docs/handoff/2026-07-24-qwen-lanes-polish-REPORT.md` committed: per work item the diff, regression test, evidence (W3: verbatim CLI evidence; W5: screenshot paths); gate lines verbatim; live proof; rev-parse hashes. Discord via `venv/bin/hermes send -t discord` — ⚠️ guard hook keyword-blocks rollout verbs ("Push"/"deploy") in the text, write "Fork aktualisiert"/"live gebracht". If a work item turns out unexpectedly large or design-ambiguous, ship the others and hand the open one back in the report instead of forcing it.
