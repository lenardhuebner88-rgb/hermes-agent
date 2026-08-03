# herdr migration brief

Detail spec for the `/goal` run that replaces tmux with **herdr** as the terminal backend and
lifts the `/control` Terminal tab by four levels. The goal contract points here; this file is the
contract's body. Read it fully before the first edit.

Status of the facts below: gathered 2026-08-03 from herdr's docs + GitHub metadata and from this
repo. **herdr was not installed or executed at the time of writing** — every behavioural claim
about herdr is vendor documentation, not measurement. Phase 1 exists to turn that into evidence.

---

## 0. Why this is risky, and what makes it survivable

herdr is four months old (repo created 2026-03-27), at **v0.8.0**, with ~109 open issues and a
weekly release cadence. tmux 3.4 is fifteen years hardened. Two consequences drive the whole plan:

- **A herdr server restart kills the pane processes.** Snapshot restore brings back the *shape*
  (workspaces, tabs, cwd), not the running agents. "Live handoff" is explicitly experimental. So
  every herdr update is a risk to running agents — which is exactly what our nightly loops are.
- **Only one writable controller owns a terminal at a time.** Our tab currently hardwires
  `isolated=1` so each browser tab gets its own tmux client with its own viewport. herdr has no
  direct equivalent: many read-only observers, but one writer.

Therefore the end state is **herdr as the default, tmux retained as a switchable fallback
implementation behind one flag**. That is not a hedge against finishing the migration — it is the
rollback, and it must stay green in CI.

## 1. Migration surface (measured, not guessed)

tmux is not one module. Production call sites, by tmux-reference count:

| File | refs | Role |
|---|---:|---|
| `hermes_cli/projects_overview.py` | 73 | project/session overview |
| `hermes_cli/agent_terminals.py` | 55 | **the core** — dashboard Terminal tab service |
| `mcp_serve.py` | 25 | MCP-exposed terminal tools |
| `tools/voice_live_tools.py` | 22 | voice-driven terminal control |
| `hermes_cli/execution_facts_terminal.py` | 20 | execution-facts terminal probes |
| `scripts/hermes-question-hook.py`, `scripts/execution_facts.py` | — | hooks/probes |
| `scripts/tmux-stamp-identity.sh` | — | stamps `@hermes_session_id/@hermes_task_id/@hermes_kind` |
| `scripts/prune-stale-worktrees.sh` | — | checks whether a live tmux window still holds a worktree |
| `scripts/preview-realdata.sh` | — | dev fixture, seeds a fake tmux session for UI previews |

Tests carry ~350 further references (`tests/hermes_cli/test_agent_terminals.py` alone has 148).

Not in scope, and do not "fix" it: **kanban workers do not use tmux.** `_default_spawn` in
`hermes_cli/kanban_db.py` starts a plain subprocess; tmux appears there only when cleaning up dead
swarm sessions. Migrating the worker spawn path into herdr panes is a separate, later decision.

Resolve every symbol location at the moment you need it (`codegraph query <symbol>`, or
`ast.parse` + `lineno`). **Never copy a line number out of this file or any other doc.**

## 2. The herdr interfaces this migration stands on

- **`terminal session control <target>`** — writable stream. Emits newline-delimited JSON
  `terminal.frame` records (base64 ANSI bytes) and reads NDJSON commands on stdin:
  `terminal.input`, `terminal.resize`, `terminal.scroll`, `terminal.release`. One controller per
  terminal; `--takeover` replaces it. This replaces today's `PtyBridge.spawn(tmux attach-session)`.
- **`terminal session observe <target>`** — read-only stream, same frame records, unlimited
  concurrent observers. This replaces the fleet overview's 25-line `capture-pane` polling.
- **Socket API** (~80 dot-notation methods: `pane.*`, `agent.*`, `workspace.*`, `events.*`).
  Print the schema for the installed binary with `herdr api schema --json` — treat that output as
  the source of truth over anything in this file.
- **`agent.wait` / `agent wait --until blocked|idle|done`** — server-owned, event-driven, pins the
  resolved pane occupant so a replacement process cannot satisfy the wait.
- **`pane.report_metadata`** with `--token` — replaces `tmux set-option -w @hermes_task_id`.
- **`--kind hermes`** exists: Hermes Agent is a recognised agent kind (screen manifest + session).
  Also available: `claude`, `codex`, `kimi`, `grok`, `opencode`, `cursor`, and others.

Agent state vocabulary: `idle`, `working`, `blocked`, `done`, `unknown`. Note `done` is the same
underlying idle state as `idle`, but for work that finished while its tab was *unseen* — reading a
pane through the CLI does **not** mark it seen. The tab's "who is waiting on me" sort depends on
getting this distinction right.

## 3. Phases

Work in this order. Each phase ends with its gates green and a one-paragraph progress log.

### Phase 0 — Backup and rollback, before any migration edit

1. `git status --short` first. Foreign uncommitted work stays untouched.
2. Annotated backup tag on the pre-migration commit: `pre-herdr-20260803`.
3. Dump the current tmux state to `docs/refactor/reports/tmux-state-pre-herdr.txt`
   (`tmux list-sessions`, `list-windows -a`, `list-panes -a -F` with the `@hermes_*` options).
4. Introduce the backend flag **before** writing any herdr code:
   `HERMES_TERMINAL_BACKEND=tmux|herdr`, default `tmux`.
5. Write `scripts/rollback-terminal-backend.sh`: switches the flag to `tmux`, restarts
   `hermes-dashboard.service`, and verifies the tab serves a real payload. **Test the rollback
   script for real in Phase 5 — a rollback that was never executed is not a rollback.**

### Phase 1 — Install herdr and prove the bridge before committing to it

Install via `curl -fsSL https://herdr.dev/install.sh | sh` (review the script first) or
`cargo install`. Pin the exact version and record it in the brief's report file.

Then, before touching `agent_terminals.py`, prove the two claims this migration depends on, in a
throwaway session, and write the evidence to `docs/refactor/reports/herdr-bridge-probe.md`:

- **P1** `terminal session control` round-trips: a byte written to stdin appears in the pane, and
  a `terminal.frame` carries it back. Include the literal NDJSON exchange.
- **P2** The one-writer constraint's actual blast radius: attach two controllers to one terminal
  and record precisely what the second one gets.

**If P1 fails, stop and pause the goal.** The whole migration rests on it.

### Phase 2 — Backend protocol and the herdr implementation

New fork-owned module, e.g. `hermes_cli/terminal_backends/` — **never** put fork code into an
upstream-owned file. Define the protocol from the operations `agent_terminals.py` already performs
(list windows, attach, capture, send keys, create, respawn, rename, kill, detach client, options),
implement `TmuxBackend` by moving today's code behind it unchanged, then add `HerdrBackend`.

`TmuxBackend` must keep passing the existing test suite untouched. If a test has to change to keep
tmux green, that is a signal the protocol is wrong — fix the protocol.

### Phase 3 — Migrate the remaining call sites

All files from §1, in descending reference count. `projects_overview.py` is the largest and is
easy to underestimate. Each file moves to the protocol; no direct `tmux` invocation survives in
production code outside `TmuxBackend`.

`preview-realdata.sh` needs a herdr equivalent or an explicit note that previews stay tmux-based.

### Phase 4 — Redesign the Terminal tab greenfield, then lift it by four capability levels

Two orthogonal axes here; do not conflate them. **Capability levels** (1-4) are what the tab can
*do*. **Design rounds** (4a-4e) are how the interface gets *designed*. The tab is redesigned from
scratch — this is not a retrofit of the current view, and "it already works" is not a reason to
keep a layout.

#### Design constraints that are not up for redesign

`web/src/control/DESIGN.md` is binding, tokens live in `web/src/control/theme.css`, and
`scripts/gate-frontend.sh` enforces a ratchet over both. Greenfield applies to **layout,
information hierarchy, and interaction model** — not to the token system. If a design genuinely
needs the design language extended, that is an operator decision: **pause and ask**, do not
quietly widen `DESIGN.md`.

#### Mobile and desktop are equal targets

The operator drives this dashboard from a phone over Tailscale as a first-class case, not as a
fallback. The current tab has concrete, measured mobile failures that the redesign must actually
solve rather than inherit:

- Touch scrolling only works while the app under the terminal has mouse tracking on; otherwise
  `onTouchMove` sends nothing and the gesture dies, leaving a scroll button as the workaround.
- There is no native text selection on touch — selection is a separate overlay mode showing a
  *frozen* `capture-pane` snapshot instead of the live buffer.
- The pane layout is a desktop construct (1/2/4 panes); on a phone, four panes is not a layout.

Design the phone case first for at least one of the competing drafts, and state for every draft
what happens at a 390px viewport. A design that only reads well on a wide screen is not done.

#### Design rounds — run all of them, in order

- **4a Divergence.** At least **three** genuinely different drafts as HTML mockups, each covering
  desktop *and* phone. Different interaction models, not three skins of one idea — e.g. a
  status-first queue ("who is waiting on me"), a spatial workspace map, and a single-agent focus
  view with the herd as periphery. Put them on the Hermes design board so they can be viewed
  remotely; inline tool images are invisible to the operator on a phone.
- **4b Critique and choice.** Judge each draft against the design language, the mobile failures
  above, and the four capability levels below — every level must have a home in the layout. Then
  **pause and let the operator choose.** This is a taste decision and it is not the agent's to
  make alone.
- **4c Refinement.** Build out the chosen draft, grafting the best ideas from the runners-up.
  Real data in the mockup, never Lorem — a status board full of placeholder text hides exactly the
  density problems it exists to expose.
- **4d Build.** Implement the chosen design with capability levels 1-4 (below).
- **4e Acceptance.** Verify in a real browser at desktop *and* phone viewports, against the
  acceptance criteria, with console and network checked. Screenshots are evidence; assertions are
  not. Note that a background browser tab pauses polling (`document.hidden`) — do not mistake a
  paused tab for a broken stream.

Iterate 4c-4e rather than declaring the first build finished. If acceptance reveals the layout was
wrong, going back to 4c is the expected move, not a failure.

#### Capability levels

- **Level 1 — Zustandstafel.** Every pane carries `working`/`blocked`/`idle`/`done`. The tab sorts
  by *who is waiting on me*: blocked first. A badge on the tab shows the blocked count. This
  replaces reading pane after pane by hand.
- **Level 2 — Steuerpult.** Start and prompt agents from the dashboard (`agent.start`,
  `agent.prompt`) instead of only typing into an attached terminal. The fleet overview streams
  live via `terminal session observe` instead of polling `capture-pane`.
- **Level 3 — Task coupling.** Kanban task ↔ pane, carried in `pane.report_metadata` tokens and
  rendered in the tab; worktrees grouped as herdr workspaces; a push notification when an agent
  goes `blocked`. The existing `@hermes_task_id` stamping migrates here.
- **Level 4 — Orchestration and recovery.** Agents wait on each other through server-side
  `agent.wait` rather than send-keys-and-hope; chain progress is visible in the tab; after a herdr
  server restart the tab shows what was restored and what was lost, honestly, instead of showing
  a dead pane as live.

Levels 1-2 must be usable on their own — if Level 3 or 4 turns out to need product decisions,
pause and ask rather than inventing them.

### Phase 5 — Live end-to-end, with artefacts

Not unit tests. A real herdr server, real agents, the real dashboard. Record evidence under
`docs/refactor/reports/herdr-e2e-<date>/` — payloads and screenshots, not assertions:

1. herdr running as a `systemctl --user` unit; survives a client detach.
2. A real Hermes agent (`--kind hermes`) **and** a second agent (`claude` or `codex`) in panes.
3. **Full keyboard input path:** type a unique marker in the browser terminal; prove it arrived via
   `herdr pane read`. Include modifier chords (`ctrl+c`) and Enter — recall that send-keys+Enter is
   exactly where the tmux path silently failed to deliver for Codex/Qwen.
4. **Resize:** changing the browser viewport changes the pane geometry (`terminal.resize`).
5. **State transition:** drive an agent into an approval prompt; the tab must show `blocked`
   without manual refresh, and the blocked-count badge must update.
6. **Multi-tab:** two browser tabs on the same terminal — document the real behaviour under the
   one-writer constraint. If it degrades versus tmux's isolated clients, say so plainly.
7. **Restart survival:** restart the herdr server; record exactly what came back and what did not.
8. **Rollback drill:** run `scripts/rollback-terminal-backend.sh`, confirm the tab works on tmux
   again, then switch back. Capture both directions.
9. **Phone viewport:** repeat points 3-5 at a 390px viewport — typing, Enter, scrolling the
   buffer, and selecting text must all work on touch, and the blocked state must be readable
   without horizontal scrolling. This is the case the operator actually uses remotely.

Only after 1–9 are captured does `HERMES_TERMINAL_BACKEND` default flip to `herdr`.

## 4. Gates (verbatim, non-negotiable)

- Frontend: `scripts/gate-frontend.sh` — run it **first** in a worktree, it provisions the isolated
  deps tree. Never `npm ci` by hand, never `npx tsc`/`npx vitest` in a worktree. Never pipe the
  gate through `tail`; the exit code is the truth.
- Python: `scripts/run-affected.sh` while building; before any deploy, one collection sweep
  (`pytest --co -q tests/`) plus affected tests; plus `ruff`.
- Dashboard restart: `systemctl --user restart hermes-dashboard.service`.
- Deploy only on genuinely green gates, via `scripts/deploy_dashboard.sh` with `CONFIRMED=1`.
- Truth is the API payload, not a screenshot — a bare loopback curl returns 401 by design.

## 5. Hard limits

- **Never push to `origin`** (that is the NousResearch upstream). `piet-fork` only, fast-forward,
  never `--force`.
- New fork code goes in fork-owned modules, never in an upstream-owned file.
- Do not delete, skip, weaken, or narrow tests to make a gate pass.
- Do not refactor unrelated code; do not add dependencies beyond herdr itself.
- Do not migrate the kanban worker spawn path (§1) — out of scope.
- Do not create ADRs or Canon decision entries; those need the operator's explicit approval.
- Pause and ask if: P1 in Phase 1 fails; the one-writer constraint would visibly degrade the tab
  for the operator's normal two-tab usage; Level 3/4 needs a product decision; the operator has
  to choose among the design drafts at 4b; or a design would require extending the design
  language in `DESIGN.md`.
