# Terminal Hub remote-control runbook

Hermes terminal remote control uses tmux as the shared source of truth. The dashboard/API, MCP daemon, and external agents all talk to the same `TmuxAgentSessionService`; they must not create a second terminal-session registry or inject preset prompts/permission modes into agent CLIs.

## Services

- `claude-remote-hermes.service` remains the Claude App remote-control path. Use it when the Claude mobile/desktop app needs to connect to Claude's own remote-control surface.
- `hermes-mcp.service` exposes additive MCP tools for clients that want to inspect or steer Hermes-managed tmux panes. Existing non-terminal MCP tools remain compatible.
- Dashboard access over Tailscale uses the `/api/agent-terminals/*` routes and the attach websocket. It attaches to tmux windows rather than spawning an alternate node-pty session state.

## Terminal Hub boundary

Terminal Hub controls tmux windows only. The safe operations are:

- list tmux-backed sessions/windows
- capture recent pane text
- send literal keys to an existing pane
- return attach metadata for an existing pane
- draft a Markdown handoff from attach metadata plus pane capture

Destructive operations such as killing sessions/windows or closing panes stay outside the normal send-keys path and require a separately gated action. Terminal Hub does not pass arbitrary shell commands to tmux; send-keys is literal text only.

## MCP tools

`hermes-mcp.service` registers these additive terminal tools when the MCP server starts:

- `terminal_sessions_list()`
- `terminal_capture(session, window, start=-200)`
- `terminal_send_keys(session, window, text)`
- `terminal_attach_metadata(session, window)`
- `terminal_handoff_draft(session, window, start=-120)`

If tmux or the shared service is unavailable, the tools fail closed with a JSON error instead of creating fallback state. They do not set prompts, approval flags, or permission modes for agent CLIs.

## Dashboard/API routes

The dashboard uses the same service through:

- `GET /api/agent-terminals/sessions`
- `GET /api/agent-terminals/windows`
- `POST /api/agent-terminals/capture`
- `POST /api/agent-terminals/send-keys`
- `POST /api/agent-terminals/attach-metadata`
- `POST /api/agent-terminals/handoff-draft`
- `WS /api/agent-terminals/attach?session=...&window=...`

Attached sessions remain interactive: ordinary send-keys traffic is allowed, while detach only reaps the attach client and must not close the underlying tmux window.
