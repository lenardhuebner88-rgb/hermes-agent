# Buzz agent cleanup: operator wiring

These versioned user units replace the current bulk-restart unit after this
change has reached the trusted main-agent checkout. Repository workers must not
copy, reload, enable, start, or otherwise mutate the live user units.

The service uses the release runtime at
`$HOME/.hermes/hermes-agent/venv/bin/python` and runs the same standalone
`hermes_cli.buzz_agent_cleanup` worker as the dashboard. Both paths construct
`CleanupService` and therefore contend on exactly
`$XDG_RUNTIME_DIR/hermes/buzz-agent-cleanup.lock`. The unit pins
`XDG_RUNTIME_DIR` to systemd's `%t`, the user runtime directory. There is no
second `flock` wrapper: if the dashboard or timer already holds the worker lock,
the other invocation exits without starting another cleanup.

The dashboard endpoint is always all-or-nothing and accepts no unit selection.
The `--canary` option is exclusively an operator CLI path.

## Installation after merge

Run these commands from the trusted checkout as the interactive user. Keep the
backup until the canary and full run have both succeeded.

```bash
set -eu
repo="$HOME/.hermes/hermes-agent"
unit_dir="$HOME/.config/systemd/user"
backup_dir="$unit_dir/backup-buzz-cleanup-$(date +%Y%m%dT%H%M%S)"

# 1. Verify the versioned units before touching the installed copies.
systemd-analyze --user verify \
  "$repo/scripts/systemd/buzz-agents-nightly-restart.service" \
  "$repo/scripts/systemd/buzz-agents-nightly-restart.timer"

# 2. Preserve the currently installed service and timer verbatim.
install -d -m 0700 "$backup_dir"
cp --archive \
  "$unit_dir/buzz-agents-nightly-restart.service" \
  "$unit_dir/buzz-agents-nightly-restart.timer" \
  "$backup_dir/"

# 3. Install the reviewed repository copies, then reload the user manager.
install -m 0644 \
  "$repo/scripts/systemd/buzz-agents-nightly-restart.service" \
  "$unit_dir/buzz-agents-nightly-restart.service"
install -m 0644 \
  "$repo/scripts/systemd/buzz-agents-nightly-restart.timer" \
  "$unit_dir/buzz-agents-nightly-restart.timer"
systemctl --user daemon-reload

# 4. Dry-run/read-only preflight: discovery and status, no restart.
"$repo/venv/bin/python" -m hermes_cli.buzz_agent_cleanup --status

# 5. Operator-only canary. This validates the complete target set first and
#    restarts only fable.
"$repo/venv/bin/python" -m hermes_cli.buzz_agent_cleanup --canary fable

# 6. Only after a successful canary, run the full all-or-nothing worker once.
systemctl --user start buzz-agents-nightly-restart.service
systemctl --user status --no-pager buzz-agents-nightly-restart.service
```

Copying the timer preserves its existing enabled state and its contract:
daily at 04:00, up to five minutes randomized delay, and no catch-up run. Check
that state without changing it:

```bash
systemctl --user is-enabled buzz-agents-nightly-restart.timer
systemctl --user list-timers --all buzz-agents-nightly-restart.timer
```

## Rollback

If verification, preflight, canary, or the full run fails, restore both files
from the backup directory created above and reload the user manager. Do not
proceed from a failed canary to the full run.
