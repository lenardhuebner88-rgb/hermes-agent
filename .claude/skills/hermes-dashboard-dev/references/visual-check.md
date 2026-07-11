# Visual check & serving — Hermes /control dashboard

Lazy-detail for the dashboard skill. A browser is **not** needed to build or land a slice — the
tsc+vitest+build gates are the acceptance. A screenshot is optional end-of-task confirmation, not a
blocker. Don't burn iterations on rendering if the gates are green.

## Use `chromium-shot`, NOT `chromium` (Snap trap)

On this machine `chromium`/`chromium-browser` are **only the Snap** (`/snap/bin/chromium` →
`/usr/bin/snap`). The Snap runs in its own mount namespace with a **private `/tmp`**, so a screenshot
it "writes" is invisible to the host shell — `test -s` fails, and it looks like a flaky/too-eager
smoke flag or a broken screenshot. It is neither; it's confinement. Don't chase it with longer waits
or `~/snap/chromium/common` hacks.

Use the unconfined Playwright binary via the wrapper (already on PATH at `~/bin/chromium-shot`, points
at `~/.cache/ms-playwright/.../chrome-headless-shell`, writes straight to the real `/tmp`):
```bash
chromium-shot --screenshot=/tmp/control.png --window-size=1280,900 \
  "http://127.0.0.1:9119/control"
```
See `[[feedback_chromium_snap_codex_screenshots]]`.

## Auth reality (login-gated — verified 2026-07-03)

`should_require_auth()` keys off the **bind host**, not the client. The service binds `--host 0.0.0.0`
(for Tailscale `:9443`) → it is **gated even on `127.0.0.1`**: `GET /control` → **302 `/login`**
(live-verified 2026-07-03; the earlier `--insecure` phase from 2026-06-17 is OVER — the login gate
returned ~2026-06-26 and the login flow got fixed 2026-07-03).

Two traps that make agents wrongly conclude "no auth needed":
- `ps`/ExecStart still shows `--insecure` — it is an **inert legacy artifact** (the unit file comment
  says so itself); do not infer open access from it. A fresh `curl /control` (302) is the truth.
- A long-running shared Playwright browser may hold a **warm session cookie** from an earlier login —
  reaching the SPA without logging in proves the cookie, not an open gate.

Login recipe (works for Playwright and for API smoke tests):
- UI path: open `/login`, fill the standard form; credentials come from
  `HERMES_DASHBOARD_USERNAME` / `HERMES_DASHBOARD_PASSWORD` in `~/.hermes/.env` (never log them).
- API path: `POST /auth/password-login` with JSON
  `{"provider": "basic", "username": …, "password": …, "next": "/control"}` → 200 `{ok:true}`,
  session cookie; reuse the cookie jar for `/api/*` calls (bare loopback curl without it = 401/302).
- Template script: `scripts/smoke_health_status_auth.py` (cookie-jar login → gated endpoint).

`usePolling` still pauses when `document.hidden`, so keep the tab foregrounded when testing.

## Prefer the Playwright MCP for live checks

`chromium-shot` is one-shot and often captures the SPA mid-load (just a spinner + the "Control" header).
For a real visual check drive the **Playwright MCP** (registered user-scope in `~/.claude.json`):
`browser_navigate` → `browser_wait_for` → `browser_snapshot` / `browser_take_screenshot`, plus
`browser_console_messages` / `browser_network_requests` for console+network. It reuses the installed
chromium-1223. See `[[reference_playwright_mcp_visual_check]]`. `chromium-shot` stays as a quick fallback.

## Serving / phone

The dashboard is served by the running Hermes web server (port 9119); reach it on the phone via the
Tailscale serve setup — see `[[project_hermes_dashboard_tailscale]]`. **Operate/restart it via Hermes,
not by hand.**
