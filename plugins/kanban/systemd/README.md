# Kanban systemd units

## `hermes-kanban-dispatcher.service` (DEPRECATED)

Standalone dispatcher daemon. The dispatcher now runs **inside the gateway** by
default (`kanban.dispatch_in_gateway=true`). See the header comment in the unit
file before using it — running this **and** a gateway dispatcher races the same
`kanban.db` and is not supported.

## `hermes-kanban-dispatcher-watchdog.{service,timer}` — alert-only watchdog

A `--user` timer that runs `scripts/kanban_dispatcher_watchdog.py` on a ~5 min
cadence. The gateway's dispatcher writes a heartbeat after every tick to
`~/.hermes/state/kanban_dispatcher_heartbeat.json`; the watchdog reads it and
posts **one Discord alert per calendar day** to #hermes-oc when:

- `last_tick_at` is older than the threshold (default **15 min**), **or**
- `tick_health != "ok"`, **or**
- the heartbeat file is missing / unreadable.

A fresh, healthy heartbeat → no alert. **Alert-only**: it never restarts the
gateway or the dispatcher. Idempotency state lives in
`~/.hermes/state/kanban_dispatcher_watchdog_state.json` (`last_alert_bucket` =
UTC date).

These files are **templates**. They are NOT armed automatically — installing and
enabling them is a deliberate operator action.

## `strategist-harvest-watch.{service,timer}` — backlog watchdog

A conservative `--user` timer for the strategist disposition harvest watchdog.
The timer fires 15 minutes after boot and every 30 minutes thereafter. The
service runs:

```bash
/usr/bin/flock -n /tmp/hermes-strategist-harvest-watch.lock \
  /home/piet/.local/bin/hermes vision strategist --mode harvest-watch
```

`flock -n` prevents parallel harvest-watch runs; an overlap exits without
queueing another run. The CLI itself keeps the existing backlog
threshold/rearm/cooldown gates and performs no deploy, restart, push, or direct
task creation.

Like the dispatcher watchdog units, these files are **templates**. They are NOT
armed automatically — installing and enabling them is a deliberate operator
action.

### Install harvest-watch (operator)

```bash
# 1. Copy the templates into the user systemd dir.
cp plugins/kanban/systemd/strategist-harvest-watch.service \
   plugins/kanban/systemd/strategist-harvest-watch.timer \
   ~/.config/systemd/user/

# 2. If your checkout or hermes executable is not at Piet's default paths, edit
#    WorkingDirectory/ExecStart in the copied .service file.

# 3. Dry-run manually through the same CLI path before arming the timer.
hermes vision strategist --mode harvest-watch --json

# 4. Arm the timer.
systemctl --user daemon-reload
systemctl --user enable --now strategist-harvest-watch.timer

# 5. Verify.
systemctl --user list-timers strategist-harvest-watch.timer
systemctl --user status strategist-harvest-watch.service
```

### Disable harvest-watch

```bash
systemctl --user disable --now strategist-harvest-watch.timer
```

### Install (operator)

```bash
# 1. Copy the templates into the user systemd dir.
cp plugins/kanban/systemd/hermes-kanban-dispatcher-watchdog.service \
   plugins/kanban/systemd/hermes-kanban-dispatcher-watchdog.timer \
   ~/.config/systemd/user/

# 2. If your checkout is not /home/piet/.hermes/hermes-agent, edit the ExecStart
#    path in the copied .service file.

# 3. Dry-run once to confirm it reads the heartbeat and would alert sanely.
python3 scripts/kanban_dispatcher_watchdog.py --dry-run

# 4. Arm the timer.
systemctl --user daemon-reload
systemctl --user enable --now hermes-kanban-dispatcher-watchdog.timer

# 5. Verify.
systemctl --user list-timers hermes-kanban-dispatcher-watchdog.timer
systemctl --user status hermes-kanban-dispatcher-watchdog.service
```

### Tuning

`--stale-after-min` (default 15) and `--channel` can be appended to the
`ExecStart=` line. Requires `DISCORD_BOT_TOKEN` in `~/.hermes/.env`.

### Disable

```bash
systemctl --user disable --now hermes-kanban-dispatcher-watchdog.timer
```
