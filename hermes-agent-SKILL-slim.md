---
name: hermes-agent
description: "Configure, extend, or contribute to Hermes Agent."
version: 2.1.0
author: Hermes Agent + Teknium
license: MIT
metadata:
  hermes:
    tags: [hermes, setup, configuration, multi-agent, spawning, cli, gateway, development]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [claude-code, codex, opencode]
---

# Hermes Agent

<!-- SLIM build: operational core only. Deep architecture + the Known Pitfalls live in
     ~/.hermes/hermes-agent/AGENTS.md. Session-specific deep-dives live in references/.
     This file was trimmed from 11.8k → ~4k tok; do not re-inflate with one-off lore —
     route that to AGENTS.md, the Vault, or a references/ file. -->

Hermes Agent (Nous Research) is a provider-agnostic AI agent that runs in the terminal, on 15+
messaging platforms, and in IDEs — same category as Claude Code, Codex, OpenClaw. It self-improves
via **skills**, keeps **persistent memory**, runs isolated **profiles**, and is extensible through
plugins, MCP servers, custom tools, webhooks, and cron.

**Use this skill** to set Hermes up, configure features, spawn extra instances, troubleshoot, and
find the right command/setting. **Architecture & contribution depth → `AGENTS.md` (see last section).**

**Docs:** https://hermes-agent.nousresearch.com/docs/

## ⛔ Profile targeting (read first — bites every multi-profile worker)
The global `-p <profile>` / `--profile <profile>` flag MUST come **before** the subcommand — it sets
`HERMES_HOME` for that invocation (resolver `hermes_cli/main.py`). `hermes skills uninstall x -p coder`
hits the **wrong** profile; `hermes -p coder skills uninstall x` is correct.

## Vault coordination pitfall (Piet's homeserver)
For `/home/piet/vault/_agents/_coordination`, treat open sessions as a YAML-frontmatter fact: use
`/home/piet/vault/_agents/_shared/scripts/coordination-open-sessions.py`, not raw text search. In prose
say "open-session marker" / "stale coordination claim" so closeout patches don't match both frontmatter
and prose.

---

## CLI essentials
Full surface: `hermes --help`, any `hermes <cmd> --help`, or the [CLI reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands).
The 80/20:

```bash
hermes                       # interactive chat (default subcommand)
hermes chat -q "…"           # single non-interactive query (-m model, --provider, -Q quiet)
hermes setup [section]       # wizard: model|terminal|gateway|tools|agent
hermes model                 # interactive model/provider picker
hermes doctor [--fix]        # check deps + config
hermes status [--all]        # component status
hermes config edit|set KEY VAL|path|env-path|check
hermes tools [enable|disable NAME|list]      # toolsets (take effect on /reset, not mid-session)
hermes skills list|search|install|inspect|uninstall|browse
hermes curator status|archive|restore|unpin|pin|backup|rollback   # skill lifecycle (see AGENTS.md › Curator)
hermes mcp serve|add|remove|list|test
hermes gateway run|install|start|stop|restart|status|setup
hermes sessions list|browse|export|prune
hermes cron list|create|edit|pause|resume|run|remove
hermes profile list|create|use|show|export|import
hermes auth add|list|remove|reset          # credential pools
hermes plugins / memory / honcho / insights / update / acp
```

Global flags worth knowing: `-r/--resume`, `-c/--continue`, `-w/--worktree` (parallel-agent isolation),
`-s/--skills`, `--yolo`.

**Cron pitfall:** `--script` only accepts filenames under `~/.hermes/scripts/` (no abs/`~` paths) — use
a wrapper. Delivery flag is `--deliver` (not `--delivery`). `every 15m` = recurring; bare `15m` = one-shot.
Scheduled gateway "silent OK" final response must be exactly `[SILENT]`. Verify with live `hermes cron create --help`.

**Slash commands (in-session):** type `/help` for the full set. Most-used: `/new` `/model` `/reasoning`
`/skill <name>` `/tools` `/compress` `/rollback` `/yolo` `/background` `/branch`. Gateway-only: `/approve`
`/deny` `/restart` `/sethome`.

---

## Key paths
```
~/.hermes/config.yaml    main config        ~/.hermes/.env        API keys / secrets
$HERMES_HOME/skills/     installed skills   ~/.hermes/sessions/   transcripts
~/.hermes/auth.json      OAuth + cred pools ~/.hermes/logs/       gateway/error logs
~/.hermes/hermes-agent/  source (git)
```
Profiles live under `~/.hermes/profiles/<name>/` with the same layout. **Config → `config.yaml`,
secrets → `.env`.** Provider list (20+) and config sections: `hermes model` /
[config docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration).

## SOUL.md vs AGENTS.md
`SOUL.md` (slot #1 of the system prompt, loaded only from `$HERMES_HOME/SOUL.md`) = durable identity,
role boundaries, evidence posture, high-level approval gates — keep it a compact kernel, **not** a
runbook. `AGENTS.md`/`.hermes.md` = project architecture, commands, paths, workflows. Startup-context
optimization: `references/context-file-optimization.md`.

---

## Security & privacy toggles (most need a fresh session / restart)
- **Secret redaction** — OFF by default. `hermes config set security.redact_secrets true` → masks
  key-like strings in tool output. **Restart required** (snapshotted at import; can't be flipped
  mid-session by design). Independent of YOLO.
- **PII redaction (gateway)** — `hermes config set privacy.redact_pii true` (hashes user IDs, strips
  phone numbers).
- **Command approval** — `approvals.mode`: `manual` (default, prompts on destructive cmds) · `smart`
  (aux-LLM auto-approves low-risk) · `off` (= `--yolo`). Per-run bypass: `hermes --yolo …` or
  `export HERMES_YOLO_MODE=1`. YOLO does **not** disable secret redaction.

## Spawning extra Hermes instances
`delegate_task` = quick bounded subtask (shared process). A spawned `hermes` process = fully
independent, long-running, full tool access.
```bash
terminal(command="hermes chat -q 'Research X and write ~/out.md'", timeout=300)   # one-shot
terminal(command="hermes chat -q '…'", background=true)                            # long
# Interactive needs a real PTY → use tmux:
terminal(command="tmux new-session -d -s a1 -x 120 -y 40 'hermes -w'", timeout=10)
terminal(command="sleep 8 && tmux send-keys -t a1 'Build a FastAPI auth service' Enter", timeout=15)
terminal(command="sleep 20 && tmux capture-pane -t a1 -p", timeout=5)
```
Use `-w` (worktree) for code-editing agents to avoid git conflicts; prefer `cronjob` over spawning for
scheduled work.

---

## Troubleshooting (quick triage; deep recipes → references/)
- **Changes not taking effect** — tools/skills: `/reset`. config: gateway `/restart`, CLI relaunch.
  code: restart the process.
- **Tool missing** — `hermes tools` (enabled for this platform?), check `.env` env vars, `/reset`.
- **Model/provider** — `hermes doctor`, `hermes login` (re-auth OAuth), verify `.env` key. MiniMax /
  OpenRouter / Copilot-403 specifics → `references/gateway-discord-minimax-diagnostics.md`.
- **Gateway** — logs first: `journalctl --user -u hermes-gateway -n 120`. Dies on logout →
  `loginctl enable-linger $USER`. Crash loop → `systemctl --user reset-failed hermes-gateway`.
  **Restart drains** (may time out the calling tool while systemd finishes moments later) — verify
  `ActiveEnterTimestamp`/`MainPID`/`Result` before assuming failure; for immediate apply use
  `hermes gateway stop; sleep 2; hermes gateway start`. Token looks valid but offline → validate the
  `.env` token against Discord (`GET /users/@me`) before (re)starting.
- **Discord bot silent** — enable Message Content Intent. **Slack only in DMs** — subscribe
  `message.channels`. **Windows 400 "No models"** — save `config.yaml` as UTF-8 *without* BOM.
- **Aux models silent** — `auto` provider has no backend: set `OPENROUTER_API_KEY`/`GOOGLE_API_KEY` or
  pin `auxiliary.<task>.provider/model`.

### Version / update audit
Distinguish **release version** from **git-main drift** before recommending an update (read-only first):
```bash
hermes --version
git -C ~/.hermes/hermes-agent rev-list --left-right --count HEAD...origin/main 2>/dev/null || true
```
Latest release == installed but `origin/main` ahead → **post-release main-branch drift**, not a failed
install. **Never** run `hermes update` without explicit approval; treat an approved update as a
backup+receipt mutation. Full pattern + memory-hygiene routing →
`references/hermes-update-and-memory-hygiene-2026-05-06.md`,
`references/feature-adoption-roadmaps.md`.

### Memory / learning routing (Piet's Honcho+Vault setup)
Curate over raising limits; route durable learnings by layer (Honcho / compact `MEMORY.md` /
class-level skill / Vault receipt / `session_search`) — don't refill `USER.md`/`MEMORY.md` with
material that belongs elsewhere; never store drift-prone state (versions/ports/branches/secrets) as
memory. Curator safe-mode, Honcho profile-card fix, governance rollout →
`references/curator-safe-mode-pinning.md`, `references/honcho-profile-card-observer-target-fix.md`,
`references/memory-routing-governance-action2-2026-05-06.md`,
`references/memory-system-nextlevel-plan-2026-05-06.md`.

---

## Architecture & contributing → AGENTS.md
The deep material that used to live inline here is canonical in **`~/.hermes/hermes-agent/AGENTS.md`**
(don't duplicate it — point to it):

| Need | AGENTS.md section |
|------|-------------------|
| Repo layout / file-dependency chain | *Project Structure*, *File Dependency Chain* |
| Core conversation loop | *AIAgent Class (run_agent.py)* › *Agent Loop* |
| Add a tool / slash command | *Adding New Tools*, *CLI Architecture* |
| Config & env vars | *Adding Configuration* |
| Skills / toolsets / delegation / curator / cron / kanban | sections of the same name |
| **The 9 Known Pitfalls** (prompt caching, role alternation, `get_hermes_home()` …) | *Known Pitfalls* |
| Profiles / multi-instance | *Profiles: Multi-Instance Support* |
| Running tests | *Testing* |

Non-negotiables when editing the codebase (full list in AGENTS.md › *Known Pitfalls*): **never break
per-conversation prompt caching**; **strict message-role alternation**; **always use
`get_hermes_home()`** (from `hermes_constants`) for HERMES_HOME paths — never `Path.home()/".hermes"`;
new tools need a `check_fn`. Config → `config.yaml`, secrets → `.env`. Commit types:
`fix|feat|refactor|docs|chore`.
