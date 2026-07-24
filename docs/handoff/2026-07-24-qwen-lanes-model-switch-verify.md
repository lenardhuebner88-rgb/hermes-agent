# Handoff — Qwen 3.8: /lanes model-switch verification & repair

**From:** Claude Code (session b064c8dc, 2026-07-24) · **To:** interactive qwen3.8-max-preview session (Claude-Code harness, tmux `work:qwen38-lanes`)
**Operator directive (Piet):** test all model switches within the /lanes model platform, explore the failures, correct them **in this separate worktree**, and make sure all real model calls use **his subscriptions (Abo)** — never metered lanes.
**Done-when:** every model switch and every reasoning change offered by /lanes actually works — for all catalog models the config path (save / activate / reasoning toggle) succeeds or is honestly disabled in the UI, and the failures you found are fixed here with regression tests and green gates. You do NOT merge/deploy — you hand a reviewed-ready branch back.

## 0. Where you are

- Worktree: `~/.hermes/hermes-agent/.claude/worktrees/qwen-lanes-verify` · branch `qwen/lanes-model-verify` · base `ef357c392` (main incl. the shipped lanes platform `b220cd25a`).
- The live checkout `~/.hermes/hermes-agent` is edited by parallel sessions — **never edit it, never run gates there.**
- Feature background: `docs/handoff/2026-07-24-lanes-model-platform.md` (this repo) — architecture, S1 fields (`sinnvoll`, `admitted`, `reasoning_support`), probe endpoints, known deferred follow-ups.

## 1. Hard rules (cage — non-negotiable)

1. **No push, no deploy, no service restart, no `~/.hermes/config.yaml` / `.env` / credential edits.** Commit only in this worktree on `qwen/lanes-model-verify`.
2. **Abo-only for real model calls.** Allowed providers for probes: `openai-codex` (Codex sub), `kimi-coding` (Kimi Abo), `claude-cli` (Claude Max), `alibaba-token-plan` (your own seat). **Forbidden:** `openrouter`, `anthropic`, `kimi` (metered Moonshot API ≠ kimi-coding!), `neuralwatt`, `gemini`, `copilot`, `minimax`, `moa`, `nous`, `xai-oauth` — for those, config-path tests only (a save/activate/reasoning toggle makes **no** model call, so it is free and allowed for all 198 models).
3. Probes cost tokens even on Abo: sequential, small batches (`limit ≤ 8`), no retry storms.
4. **Live lane config is production routing.** Never mutate the ACTIVE lane on the live instance. Live mutation tests only via a throwaway test lane you create and delete afterwards; prefer the isolated backend (§3) for the full matrix.
5. Don't kill or write to other tmux windows/sessions; don't touch other worktrees.

## 2. Access to the live dashboard (read + probes)

Creds in `~/.hermes/.env` (`HERMES_DASHBOARD_USERNAME` / `HERMES_DASHBOARD_PASSWORD`) — never print/log them or any token.

```
POST http://127.0.0.1:9119/auth/password-login   {"provider":"basic","username":…,"password":…,"next":"/control"}
GET  /control  → extract window.__HERMES_SESSION_TOKEN__ from the HTML
→ header X-Hermes-Session-Token on all /api/… calls (bare curl = 401)
```

Endpoints: `GET /api/plugins/kanban/lanes` (full payload: lanes/profiles/models), `POST /api/plugins/kanban/lanes/model-probe`, `POST /api/plugins/kanban/lanes/catalog-probe`. Live today: 3 lanes (`lane_46bba8a4` **active** builtin, `lane_4e03437c` builtin, `lane_68121bfc` custom), 10 profiles, 198 models (68 sinnvoll).

## 3. Isolated backend for the mutation matrix

`scripts/lanes-e2e.sh` shows the pattern: disposable `HERMES_HOME`, worktree backend via `PYTHONPATH=$(pwd)` + live venv (`/home/piet/.hermes/hermes-agent/venv/bin/python`), seeded profiles, gate-built `web_dist`. Adapt it for an interactive server you can hit with the API.

**⚠️ Precondition before ANY mutating sweep:** verify DB isolation. Known trap: `HERMES_HOME` does NOT reliably isolate `kanban.db` (board path can stay pinned to `~/.hermes/kanban.db` — where lanes live). After booting your isolated backend, prove which DB file it opened (`ls -l /proc/<pid>/fd | grep kanban` or lsof). If it's the live DB → stop, set `HERMES_KANBAN_DB` to a temp copy explicitly, re-verify. If lanes turn out to live elsewhere than you assumed, write down what you found — that's a finding, not a detour.

## 4. Test matrix (the actual mission)

**A. Switch matrix (config path, free, all models):** for each profile × each catalog model (at minimum: all 68 `sinnvoll` + a sample of non-sinnvoll per provider): save the override, re-read the lane, assert the persisted value matches (provider, model, runtime, fallbacks intact). Then activate-lane round-trip. Known suspect: deferred finding **F3-1 — the locked serializer drops `fallback_providers`** on some path; if your matrix reproduces it, it is now IN scope to fix (operator said "correct the failures"). Also deferred: R2 persist-to-lane concurrency (fix only if a failure actually traces to it; keep the diff minimal).

**B. Reasoning matrix:** for every model with `reasoning_support=true`: set each offered level (STD/MIN/LOW/MED/HIGH), persist, re-read, assert. For models without support: assert the API rejects/ignores it consistently with the UI's "Modell hat keinen Reasoning-Knopf". Mismatches between `reasoning_support` flag and actual transport capability = findings.

**C. Abo probe sweep (live, real calls):** `catalog-probe`/`model-probe` over the four Abo providers only. Collect the per-model status feed (`ok / fallback / auth_error / quota_or_rate_limit / timeout / config_error / error`). Every non-`ok` gets a diagnosis: creds? catalog wiring? endpoint? entitlement? For `alibaba-token-plan`, remember only 22 models are seat-entitled — a 403 on a non-entitled ID is expected behavior, not a bug (but if the catalog marks it `sinnvoll`+probe-able, that's a catalog-curation finding).

**D. Fixes:** in this worktree, smallest-possible diffs, each with a regression test against the REAL data format. Frontend gates: `scripts/gate-frontend.sh` from the worktree root (web deps are pre-installed; use hoisted `node_modules/.bin/{tsc,vitest}` — **never `npx`** here, stub-trap ENOWORKSPACES). Python: per-file pytest only via the live venv (`PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest tests/<file> …`) — a naive full-suite run produces cross-file artifacts. Plus `ruff check .`.

## 5. Report artifact (own done-when)

Write `docs/handoff/2026-07-24-qwen-lanes-verify-REPORT.md` in this worktree and commit it:
- matrix results table (per provider: tested / passed / failed with status class),
- each failure: root cause, fix commit, regression test path,
- anything found-but-not-fixed with reasoning,
- gate evidence (verbatim exit lines, not piped through tail/grep — pipe-to-grep silently lies),
- explicit statement which DB your isolated backend used.

Commit style: `lanes-verify: <what>` + the report. Claude reviews and lands afterwards (foreign diff → independent review — that's the standing ladder, don't self-merge).

## 6. If blocked

Auth to dashboard failing / seat key 401 / worktree broken → write the blocker into the report file and stop; the operator or Claude picks it up. Don't work around by switching to metered providers — that violates rule 2.
