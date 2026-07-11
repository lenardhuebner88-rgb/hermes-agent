# browser_reap.py — operator wiring (CAP-S2)

`scripts/browser_reap.py` is the tested reaper logic; it lives in **this** repo
(`hermes-agent`) so it is reviewed and versioned. The reaper *entry point* —
`ui-preview.sh` — lives in a **different** repo (`~/.hermes/scripts/`, its own git
root) that a caged worker cannot commit to. This file is the exact, minimal wiring
for the operator/integrator to apply there after review.

## Timer — already exists, no new unit needed

`~/.config/systemd/user/ui-preview-reap.timer` already drives
`ui-preview.sh reap` (`OnBootSec=10min`, `OnUnitActiveSec=15min`, `Persistent=true`).
Extending the `reap` subcommand is therefore the whole "Timer-Anbindung": the
existing timer picks it up automatically. **Do not add a new unit.**

## The change — one line in `~/.hermes/scripts/ui-preview.sh`

Append to `cmd_reap()` (after the existing vite cleanup, before `return 0`):

```bash
  # Browser-automation leaks (Playwright-MCP / control-shot headless Chromium) —
  # same orphan class as the vite leak above. CAP-S2. FIRST ROLLOUT = DRY-RUN:
  # this only journals WOULD-KILL lines; add --apply once the journal shows the
  # candidates are all genuine orphans (AC-1: dry-run is the default first rollout).
  "$LIVE_REPO/.venv/bin/python3" "$LIVE_REPO/scripts/browser_reap.py" || true
```

`$LIVE_REPO` is already defined in `ui-preview.sh`
(`/home/piet/.hermes/hermes-agent`) and its `.venv` has `psutil` (verified 7.2.2).

### Flip to enforcing after the observation window

Once `journalctl --user -u ui-preview-reap.service` shows only genuine orphans in
the `WOULD-KILL` lines, change the wiring to enforce:

```bash
  "$LIVE_REPO/.venv/bin/python3" "$LIVE_REPO/scripts/browser_reap.py" --apply || true
```

## Manual verification (operator)

```bash
# dry-run against the live process table (signals nothing):
/home/piet/.hermes/hermes-agent/.venv/bin/python3 \
  /home/piet/.hermes/hermes-agent/scripts/browser_reap.py
# -> prints "browser-reap: done mode=dry-run scanned=<N> candidates=<M> ..."
```

## Kill criteria (three necessary conditions — all must hold)

1. **Narrow signature** — `@playwright/mcp@`, the MCP output-dir, or a `--headless`
   Chromium under the `ms-playwright` browser cache. Never a bare `chrome`/`--headless`.
2. **Age > threshold** (default 6h; `--threshold-hours`). No legitimate MCP /
   control-shot session runs that long.
3. **Orphaned** — parent is init / a `systemd --user` subreaper / already dead. A
   matched, ancient MCP whose parent is a *live* interactive `claude` session is
   **not** reaped (it is in use).

Hard-excluded regardless of the above: the Google Meet bot browser/parent
(`--use-fake-ui-for-media-stream`, `-m plugins.google_meet.meet_bot`).
