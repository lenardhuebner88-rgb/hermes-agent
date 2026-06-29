---
title: "Agent Terminal Hub Mockup"
status: draft
owner: Codex
created: 2026-06-29
mockup_html: docs/agent-terminal-hub-mockup.html
---

# Agent Terminal Hub Mockup

Goal: Hermes Dashboard/App should become the single remote control surface for
normal interactive terminals across Hermes, Claude Code, Codex, and Kimi Code.
The workflow is remote-first and terminal-native: the sessions live in tmux on
the homeserver, the phone connects over Tailscale, and Piet uses the CLIs exactly
as he would in a local tmux pane. PlanSpec/Kanban handoff is an optional side
action, not the default reason the terminal exists.

Static mockup: [agent-terminal-hub-mockup.html](agent-terminal-hub-mockup.html)

Rendered screenshots:

- [Desktop PNG](agent-terminal-hub-mockup-desktop.png)
- [Mobile PNG](agent-terminal-hub-mockup-mobile.png)

## Remote-First Requirement

The terminal hub must not depend on a browser WebSocket owning the agent
process. Phones disconnect, background tabs are throttled, and Tailscale clients
sleep. Therefore:

- tmux is the durable session host.
- Hermes Dashboard/App is an attach/control client.
- Web/xterm may disconnect at any time without killing Claude/Codex/Kimi/Hermes.
- No prompt is prefilled or auto-submitted when attaching.
- Normal typing, paste, slash commands, approvals, and CLI-native flows stay
  inside the underlying agent CLI.
- The dashboard should not insert an extra permission workflow before normal
  terminal input. Extra confirmation is only for hub-level destructive actions
  such as killing a tmux window.
- Existing `tmux work` session is the baseline:
  - `claude`
  - `codex`
  - `kimi`
- Add/ensure a `hermes` window for Hermes TUI.
- Existing remote services are part of the target topology:
  - Dashboard: `hermes-dashboard.service`, port `9119`, Tailscale Serve `:9443`.
  - MCP bridge: `hermes-mcp.service`, loopback `127.0.0.1:8765`.
  - Claude Remote Control: `claude-remote-hermes.service`.

Live-state verified 2026-06-29:

```text
tmux session work: claude, codex, kimi
work:claude -> claude in /home/piet
work:codex  -> node/codex in /home/piet
work:kimi   -> bash/kimi in /home/piet/.hermes/hermes-agent
```

## Desktop Layout

```text
+--------------------------------------------------------------------------------------+
| Agent Terminal Hub                                                                    |
+----------------------+---------------------------------------------+-----------------+
| tmux work            | Tabs: Codex Claude Hermes Kimi      actions | Optional tools  |
|                      |                                             |                 |
| [work:codex]         | +-----------------------------------------+ | Capture output  |
| [work:claude]        | | normal xterm attached to tmux           | | PlanSpec draft |
| [work:hermes]        | |                                         | | Kanban task    |
| [work:kimi]          | | no prefilled prompt, no auto-send       | |                 |
|                      | |                                         | |                 |
| Ensure Window        | +-----------------------------------------+ | Validate/Ingest |
+----------------------+---------------------------------------------+-----------------+
```

Desktop keeps three work areas visible at once:

- Left: tmux session/window registry with agent kind, cwd, status, attach count.
- Center: one active xterm attach-client to a tmux window.
- Right: optional tooling drawer for capture/handoff. It can be hidden; it never
  blocks normal terminal use.

## Mobile Layout

```text
+----------------------------------+
| Agent Terminal              [L]  |
+----------------------------------+
| Cx | Cl | He | Ki                |
+----------------------------------+
|                                  |
| real terminal                    |
|                                  |
| selected output highlighted      |
|                                  |
+----------------------------------+
| Optional capture/handoff tools   |
| Copy | Ask Hermes | Draft | Go   |
+----------------------------------+
```

Mobile collapses the session list into a drawer and moves handoff into a bottom
sheet. The terminal remains the primary surface; actions are intentionally few.

## Existing Building Blocks

- Dashboard PTY WebSocket exists for the Hermes TUI: [web_server.py](/home/piet/.hermes/hermes-agent/hermes_cli/web_server.py:12064)
- Dashboard PTY command resolution currently targets Hermes chat/TUI: [web_server.py](/home/piet/.hermes/hermes-agent/hermes_cli/web_server.py:11806)
- POSIX PTY bridge already exists: [pty_bridge.py](/home/piet/.hermes/hermes-agent/hermes_cli/pty_bridge.py:1)
- Web app already ships xterm dependencies via `web/package.json`.
- Web `/chat` already contains the mature xterm path: URL/auth wiring, channel
  IDs, xterm setup, fit/resize, copy/paste, reconnect, and mobile behavior in
  [ChatPage.tsx](/home/piet/.hermes/hermes-agent/web/src/pages/ChatPage.tsx:44).
- WebSocket auth helpers already exist in [api.ts](/home/piet/.hermes/hermes-agent/web/src/lib/api.ts:255)
  (`getWsTicket`, `buildWsAuthParam`, `buildWsUrl`).
- Chat terminal sidecar/event rendering can inform the context rail:
  [ChatSidebar.tsx](/home/piet/.hermes/hermes-agent/web/src/components/ChatSidebar.tsx:1).
- Desktop app has node-pty terminal IPC: [main.cjs](/home/piet/.hermes/hermes-agent/apps/desktop/electron/main.cjs:6238)
- Desktop renderer already has an xterm terminal tab with selection-to-chat: [index.tsx](/home/piet/.hermes/hermes-agent/apps/desktop/src/app/right-sidebar/terminal/index.tsx:14)
- Desktop terminal buffer can be read through `read_terminal`: [buffer.ts](/home/piet/.hermes/hermes-agent/apps/desktop/src/app/right-sidebar/terminal/buffer.ts:1)
- Control views are lazy-routed and can accept a new tab: [ControlPage.tsx](/home/piet/.hermes/hermes-agent/web/src/control/ControlPage.tsx:79)
- Flow/PlanSpec integration already belongs near [FlowView.tsx](/home/piet/.hermes/hermes-agent/web/src/control/views/FlowView.tsx:284).
- Existing tmux bootstrap is `/home/piet/start-work.sh`; it creates `work` with
  `claude`, `codex`, and `kimi`.
- Kimi/tmux receipt: `/home/piet/vault/03-Agents/Codex/receipts/kimi-cli-tmux-setup-2026-06-12-receipt.md`.

## Component Boundaries

- `ControlTerminalHubView`: new Control route.
- `AgentTerminalPane`: extracted xterm renderer with injected attach transport.
- `TmuxSessionController`: server-side tmux list/ensure/capture/send/attach.
- `WebTmuxAttachTransport`: browser/WebSocket transport that spawns only
  `tmux attach -t <session>:<window>` as the PTY child.
- `DesktopTmuxAttachTransport`: Electron/node-pty transport that attaches to
  tmux, not directly to agent CLIs.
- `McpTmuxControlAdapter`: MCP-facing wrapper over the same controller so
  Hermes/Claude/Codex automation uses the identical state model.
- `AgentRuntimePicker`: Hermes / Claude Code / Codex / Kimi Code.
- `TerminalToolsDrawer`: optional capture/output/Handoff tools; hidden by
  default on small screens unless selection/capture is requested.
- `TerminalSelectionHandoff`: selected-output preview plus PlanSpec/Kanban actions.
- `TerminalOutputPreview`: read-only ANSI/log preview for captured output.

## Tmux Window Resolvers

Do not blindly spawn arbitrary shell commands. Each agent kind should resolve to
a tmux session/window definition, not a browser-owned subprocess:

| Kind | Default command | Notes |
|---|---|---|
| Hermes | window `hermes`, command `/home/piet/.hermes/hermes-agent/.venv/bin/hermes --tui` | Additive window; avoid broken global `hermes` symlink. |
| Claude Code | existing window `claude`, command `/home/piet/.local/bin/claude` | Current `work:claude` already active; remote-control service remains separate. |
| Codex | existing window `codex`, command `/home/piet/.npm-global/bin/codex` | Current `work:codex` active. |
| Kimi Code | existing window `kimi`, command `/home/piet/bin/kimi-code` | Current `work:kimi` active. |
| Shell | dedicated window `shell-*` | Optional, visually separated, disabled by default. |

Bootstrap starts the CLI only. It must not send task prompts, paste canned text,
toggle permission modes, or pre-answer agent-native prompts. Any Claude/Codex/Kimi
permission prompt remains the real CLI's own prompt inside the terminal.

Each resolver should carry:

- `kind`
- `label`
- `tmux_session`
- `tmux_window`
- `bootstrap_argv`
- `cwd`
- `env`
- `allow_create`
- `allow_attach`
- `supportsResume`
- `riskClass`

Attach path:

```text
phone/browser -> Tailscale :9443 -> Hermes Dashboard
  -> WS /api/agent-terminals/tmux/{session}/{window}/attach
  -> PtyBridge spawns: tmux attach -t work:codex
  -> disconnect kills only the attach client, not the tmux window/process
```

Control path:

```text
POST capture -> tmux capture-pane -p -J -t work:codex -S -200
POST input   -> tmux send-keys -t work:codex -- <keys>
POST ensure  -> tmux has-session/new-session/new-window using allowlisted templates
```

## Handoff Model

The terminal itself should not mutate Kanban implicitly. Handoff is explicit:

1. User selects terminal text or captures last N lines.
2. Handoff drawer creates a draft.
3. Draft is either:
   - PlanSpec file with `freigabe: operator`, then `hermes plan validate`.
   - Direct Kanban task through `hermes kanban create --triage` or `--body`.
4. User sees validation result and dry-run.
5. User chooses ingest/dispatch.

Selection metadata should be structured, not a raw anonymous blob:

```json
{
  "source_agent": "codex",
  "session_id": "abc",
  "cwd": "/home/piet/project",
  "command": "npm test",
  "text": "selected terminal output",
  "line_range": [120, 148],
  "captured_at": "2026-06-29T10:25:00Z"
}
```

Direct Kanban task path should reuse `POST /api/plugins/kanban/tasks`, with
`park: true`, `triage: true`, `tenant: "terminal-handoff"`, and no immediate
dispatch.

PlanSpec draft path needs only one new adapter endpoint that writes a markdown
draft under `/home/piet/vault/03-Agents/Hermes/plans/` or the selected agent's
plans directory. After that, reuse existing PlanSpecHub APIs:

- `GET /api/plugins/kanban/planspecs`
- `GET /api/plugins/kanban/planspecs/detail?path=...`
- `POST /api/plugins/kanban/planspecs/ingest`
- `POST /api/plugins/kanban/planspecs/sprint-prompt`

Relevant commands:

```bash
/home/piet/.hermes/hermes-agent/.venv/bin/hermes plan validate <planspec.md>
/home/piet/.hermes/hermes-agent/.venv/bin/hermes plan ingest <planspec.md> --json
/home/piet/.hermes/hermes-agent/.venv/bin/hermes kanban create "Title" --body "..." --triage --json
/home/piet/.hermes/hermes-agent/.venv/bin/hermes kanban dispatch --dry-run --json
```

Safe defaults:

- `freigabe: operator`
- `live_test_depth: smoke`
- `review_tier`: suggested by risk endpoint/classifier, user can raise
- `park: true` for direct Kanban task
- release/dispatch only after explicit operator action

Handoff must be opt-in. Selecting text may reveal tools, but it must not create,
validate, ingest, dispatch, or paste anything unless Piet clicks that action.

## API Shape

Suggested backend surface:

```text
GET    /api/agent-terminals/tmux/capabilities
GET    /api/agent-terminals/tmux/sessions
POST   /api/agent-terminals/tmux/ensure
GET    /api/agent-terminals/tmux/{session}/{window}
WS     /api/agent-terminals/tmux/{session}/{window}/attach
POST   /api/agent-terminals/tmux/{session}/{window}/send-keys
POST   /api/agent-terminals/tmux/{session}/{window}/capture
POST   /api/agent-terminals/tmux/{session}/{window}/interrupt
POST   /api/agent-terminals/tmux/{session}/{window}/detach-client
POST   /api/agent-terminals/tmux/{session}/{window}/resize-client

POST   /api/agent-handoff/draft
POST   /api/agent-handoff/validate
POST   /api/agent-handoff/ingest
POST   /api/agent-handoff/dispatch-dry-run
```

Alternative minimal backend footprint:

```text
POST /api/plugins/kanban/terminal-handoffs/planspec-draft
POST /api/plugins/kanban/tasks
POST /api/plugins/kanban/planspecs/ingest
POST /api/plugins/kanban/tasks/{root}/flow-release
POST /api/plugins/kanban/dispatch?max=1
```

The existing `/api/pty` can remain the stable Hermes chat endpoint. The new API
should be parallel and tmux-backed so existing `/chat` behavior is not disturbed.

## Security Rules

- Allowlist agent kinds and command templates.
- tmux target names must be validated identifiers; no arbitrary `-t` injection.
- Allowlist cwd roots, defaulting to `/home/piet/.hermes/hermes-agent`,
  `/home/piet/vault`, and explicit project worktrees.
- No arbitrary command field in the browser payload.
- Shell sessions disabled by default and visually marked when enabled.
- Normal key input into an attached tmux pane is not permission-gated by Hermes.
- Log session start/stop/input metadata, not full secret-containing terminal
  buffer by default.
- Handoff to Kanban/PlanSpec is a separate explicit action.
- `freigabe: operator` for generated PlanSpecs unless the operator explicitly
  raises it.
- Process-spawning changes and handoff-to-worker changes should use
  `review_tier: critical`.
- Attach/disconnect must never kill the underlying tmux session. Killing an
  agent window needs a separate explicit destructive confirmation.

## Implementation Slices

1. Tmux capability + inventory module over the existing `work` session.
2. Tmux attach WebSocket + capture/send/ensure API.
3. Web Agent Terminal Hub view under `/control`.
4. Mobile/Tailscale reconnect behavior and attach-client lifecycle.
5. Normal terminal UX: type/paste/resize/copy without hub-level prompt injection.
6. Terminal selection/buffer capture + optional tools drawer.
7. PlanSpec/Kanban validate/ingest API.
8. MCP adapter over the same tmux controller.
9. Desktop reuse/bridge parity.
10. E2E and security tests.

## Verification

- Backend unit tests for resolver allowlist, cwd validation, broken symlink
  reporting, tmux target validation, attach-client lifecycle, and resize/input flow.
- Tests must prove WebSocket disconnect kills only the tmux attach client.
- Tests must prove attach does not auto-send any prompt/input to Claude/Codex/Kimi/Hermes.
- Web unit tests for session list, mobile bottom sheet, and handoff state.
- Playwright smoke at desktop width and 390px mobile width.
- Mobile/Tailscale manual smoke: open dashboard through Tailscale, attach to
  `work:codex`, background the phone/browser, reconnect, confirm the tmux pane
  continued.
- Regression tests around `tests/hermes_cli/test_pty_bridge.py`,
  `test_win_pty_bridge.py`, `test_web_server_pty_import.py`,
  `test_web_server_pty_reconnect.py`, and `test_dashboard_auth_ws_auth.py`.
- Frontend tests near `FlowView.test.tsx`, `PlanSpecDetailDrawer.test.tsx`,
  `useLiveEvents.test.ts`, and `web/e2e/control-smoke.spec.ts`.
- Desktop tests around terminal IPC/preload and composer terminal-selection
  serialization.
- Manual smoke with harmless `printf` fake PTY before launching live agents.
- Manual live smoke for one Codex/Kimi/Claude/Hermes terminal after operator
  confirms credentials and cwd.
