---
name: hermes-loops
description: 'Hermes Loop-Runner betreiben/erweitern - Packs starten/stoppen/schedulen ("starte den Nachtlauf"), Modell/Engine-Swap, Loop-Branch landen (--cmd land), Packs bauen/tunen (Werkstatt, loop-schmiede), Nachtlauf debuggen (LEDGER, heartbeat, bounced plans, usage-limit). Deckt loops/runner.py, loops/packs/, ~/.hermes/loops/-State, Loops-Tab, hermes-loop@ Units. NICHT fuer alte Standalone-Harnesses oder Kanban-Worker-Chains.'
---

# Hermes Loops — operate & extend the pack-based Loop-Runner

One runner, many loops: a **pack** (`loops/packs/<name>/pack.yaml` + prompts) says WHAT
runs at night; the runner supplies HOW (worktree isolation, queue, ledger, retry/revert,
locks, usage-limit stop). Runtime state per pack: `~/.hermes/loops/<pack>/`.
Depth (schema, engines, land rails): `loops/README.md` in the repo. Design SoT:
`~/vault/03-Agents/Claude-Code/plans/2026-07-02-loop-runner-v1-v2.md`.

## Quick reference (repo = ~/.hermes/hermes-agent)

| Task | Do this |
|---|---|
| Run once, now (detached) | `systemctl --user start --no-block hermes-loop@<pack>` — or Dashboard → Loops → Start |
| Run in foreground (debug) | `venv/bin/python -m loops.runner --pack <pack> --cmd night` (repo cwd; `night` is safe for BOTH archetypes — for sweep it just runs rounds) |
| Soft-stop (also from phone) | Dashboard Stop button, or `touch ~/.hermes/loops/<pack>/STOP` — takes effect before the NEXT phase; a running 45-min phase finishes first |
| Every night | `loops/systemd/install.sh` once, then `systemctl --user enable --now hermes-loop@<pack>.timer` (23:37; per-pack time via `systemctl --user edit`) — or Dashboard timer toggle |
| One-run model/param switch | Dashboard Start panel (per-phase engine/model selects + param fields) — writes `~/.hermes/loops/<pack>/overrides.env` (`PHASE_<PHASE>_MODEL=…`, `MAX_ROUNDS=…`, `<PARAM>=…`). Phase names are the pack's actual phases: **pipeline = PLAN/BUILD/VERIFY, sweep = ROUND** (a sweep's "builder model" is `PHASE_ROUND_MODEL`). **Never edit repo `pack.yaml` for a one-night change** |
| Permanent default change | edit `loops/packs/<pack>/pack.yaml` = repo commit + review (or loop-tuner does it evidence-based) |
| Land verified commits on main | `venv/bin/python -m loops.runner --pack <pack> --cmd land [--no-push]` or Dashboard Land button — rails: UNVERIFIED/dirty-live → Abbruch; non-ff (main weitergelaufen) wird automatisch rebased, wenn der Pack-Worktree clean ist (Rollback-Anker `loop-rebase/<pack>/<ts>`); Rebase-Konflikt/dirty wt → Abbruch; verdict lands in LEDGER (`LAND ✅` / rollback) |
| New pack | Dashboard Werkstatt "Duplizieren" → edit in browser (server lint on save), or `cp -r loops/packs/_blank ~/.hermes/loops/packs-custom/<name>` — custom packs NEVER in the repo |
| Watch a run | LEDGER: `tail ~/.hermes/loops/<pack>/LEDGER.md` · live phase: Dashboard heartbeat chip or `~/.hermes/loops/<pack>/heartbeat.json` · phase logs in `~/.hermes/loops/<pack>/logs/` |
| Read a bounced plan | `~/.hermes/loops/<pack>/queue/90-bounced/*.md` — Verifier feedback is appended inside the file |
| Read escalated BLOCKED findings | `cat ~/.hermes/loops/*/ESCALATIONS.md` — structured blocks (Evidenz/Fix-Skizze/Kanal) for real bugs the packs may not fix themselves; MUST-read in the morning review, else findings die in the ledger |

Engines (catalog `loops/models.yaml`): `claude` (slug), `kimi` (nur
`kimi-code/kimi-for-coding` — der Coding-Endpoint IGNORIERT das model-Feld, andere Namen
werden still gleich bedient), `codex`, `hermes` (**"model" = Hermes PROFILE**:
`reviewer`→NeuralWatt glm-5.2, `coder`→Codex pool), `neuralwatt` (direkter Modell-Slug via
`hermes -m <model> --provider neuralwatt -z`; kuratierte Liste — gegen `loops/models.yaml`
prüfen, nicht aus dem Gedächtnis).
New engine = module in `loops/engines/` with `@register("name")`.

## Traps (each cost a night-session once — do not relearn)

- `overrides.env` is read at **process start** only; editing it mid-run does nothing.
  It is strictly one-run: the runner renames it to `overrides.consumed.env` at
  night/run start, so a `SKIP_PLAN=1` cannot stick beyond the run it was meant for.
- codex engine runs `--sandbox danger-full-access`, because `workspace-write`
  broke worktree git commits (gitdir under main repo's `.git/worktrees/`), the
  `last-status` write (STATE_DIR outside cwd → "build-fail: ?") and tmux/loopback
  gate tests. Usage-limit detection scans only the last 4000 chars of output
  (mid-log "429"/"rate limit" strings in agent-authored code were false-stopping runs).
- Loop agents: `git add -A` BEFORE `./loops/gate.sh` — new files are invisible to
  `git diff HEAD` otherwise. `git clean` is blocked by the guard hook even headless:
  only the runner driver cleans.
- Claude CLI reports limits as "session limit" too (not just 429/usage limit) —
  detection lives in `loops/engines/__init__.py`; the loop STOPS on it by design.
- Gates in pack worktrees: `./loops/gate.sh` runs **ruff** from the live `venv/` and
  delegates **pytest** to `scripts/run-affected.sh`, which resolves its own interpreter.
  Never name an interpreter path in a pack prompt — `venv/` has no pytest.
  Test environment and its traps: `AGENTS.md` → "Python test environment".
- hermes engine runs need `HERMES_SANDBOX_MODE=1` (kanban.db is deliberately NOT
  profile-isolated) — the adapter sets it; don't remove.
- `systemctl start` on these oneshot units MUST use `--no-block` (blocks for hours
  otherwise) — relevant if you script starts yourself.
- Landing verdict about commit QUALITY stays human/main-agent: read LEDGER +
  `git log main..loop/<pack>` BEFORE `--cmd land`. Auto-land stays off (v2 ladder).
- STOP file does not touch timers — disable the timer too if the pack should pause.
- Loop `.sh` files must stay git-mode **100755**; a 100644 shim → systemd `203/EXEC`
  silent-fail (ExecStart runs it via `/bin/bash` as a backstop).
  If Start "works" but nothing runs: `systemctl --user status hermes-loop@<pack>` —
  `203/EXEC` = exec bit lost. `install.sh` re-chmods; the Start endpoint now 502s on
  a fast-fail instead of reporting a phantom "started".
