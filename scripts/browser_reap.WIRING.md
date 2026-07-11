# browser_reap.py — operator wiring (CAP-S2)

`scripts/browser_reap.py` is the tested reaper logic; it lives in **this** repo
(`hermes-agent`) so it is reviewed and versioned. The reaper *entry point* —
`ui-preview.sh` — lives in a **different** repo (`~/.hermes/scripts/`, its own git
root) that a caged worker cannot commit to. This file is the exact, minimal wiring
for the operator/integrator to apply there after review.

## Timer-Anbindung — repo-durable unit templates (preferred, CAP-S2)

The scheduling is delivered as **repo-tracked** unit templates (reviewed +
versioned, the durable artifact a cross-repo `ui-preview.sh` edit could not be):

- `scripts/systemd/agent-browser-reap.service` — `Type=oneshot`, flock-guarded,
  `ExecStart` runs the venv python on `scripts/browser_reap.py --dry-run`.
- `scripts/systemd/agent-browser-reap.timer` — `OnBootSec=10min`,
  `OnUnitActiveSec=15min`, `Persistent=true`, `WantedBy=timers.target` (same
  cadence as `ui-preview-reap.timer`).

Both are **templates**: not enabled/started here. Installation + activation is
the operator/verifier job (CAP-S6):

```bash
install -m644 scripts/systemd/agent-browser-reap.service ~/.config/systemd/user/
install -m644 scripts/systemd/agent-browser-reap.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-browser-reap.timer
```

Flip the unit's `ExecStart` from `--dry-run` to `--apply` only after the journal
observation window (below) confirms the `WOULD-KILL` lines are genuine orphans.

## Alternative — one line in `~/.hermes/scripts/ui-preview.sh`

If you prefer to fold the sweep into the existing `ui-preview-reap.timer` instead
of a sister unit (no new unit, but a cross-repo edit),

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
