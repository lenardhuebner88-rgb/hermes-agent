from datetime import datetime, timedelta, timezone

from hermes_cli.gateway import _runtime_health_lines


def _iso_age(seconds_ago: float) -> str:
    """ISO-8601 UTC timestamp ``seconds_ago`` in the past (drives _marker_is_stale)."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


_STALE_LINE_PREFIX = "⚠ Stale gateway_state.json:"


def _stale_lines(lines):
    return [ln for ln in lines if ln.startswith(_STALE_LINE_PREFIX)]


def test_runtime_health_lines_flags_stale_running_with_dead_pid(monkeypatch):
    """Stale updated_at + dead PID + 'running' -> contradiction line, no draining line."""
    from gateway import status as status_mod

    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "pid": 4242,
            "start_time": 111,
            "updated_at": _iso_age(600),  # well past the 120s TTL -> stale
            "active_agents": 0,
        },
    )
    # Recorded PID is gone (ungraceful kill); no real process is touched.
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: None)

    lines = _runtime_health_lines()

    stale = _stale_lines(lines)
    assert len(stale) == 1, lines
    assert "recorded state 'running'" in stale[0]
    assert "recorded process is gone" in stale[0]
    # The misleading live-state summary must be suppressed.
    assert not any("draining" in ln.lower() for ln in lines), lines


def test_runtime_health_lines_no_warning_for_fresh_running_with_live_pid(monkeypatch):
    """Fresh updated_at + live PID + 'running' -> no contradiction line."""
    from gateway import status as status_mod

    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "pid": 4242,
            "start_time": 111,
            "updated_at": _iso_age(5),  # fresh, within the 120s TTL
            "active_agents": 2,
        },
    )
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: True)
    # start_time matches the record -> not a recycled PID.
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: 111)

    lines = _runtime_health_lines()

    assert _stale_lines(lines) == [], lines


def test_runtime_health_lines_no_warning_for_stale_running_but_live_pid(monkeypatch):
    """Stale updated_at but PID still live (long-idle gateway) -> no false positive."""
    from gateway import status as status_mod

    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "pid": 4242,
            "start_time": 111,
            "updated_at": _iso_age(600),  # stale timestamp...
            "active_agents": 0,
        },
    )
    # ...but the recorded process is genuinely alive (start_time matches).
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: pid == 4242)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: 111)

    lines = _runtime_health_lines()

    # Guard requires BOTH stale AND dead PID; live PID must suppress the warning.
    assert _stale_lines(lines) == [], lines


def test_runtime_health_lines_treats_missing_updated_at_as_stale(monkeypatch):
    """Missing updated_at (degrade path) + dead PID + 'running' -> contradiction line."""
    from gateway import status as status_mod

    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "pid": 4242,
            "start_time": 111,
            # no "updated_at" -> runtime_status_is_stale must treat as stale
            "active_agents": 0,
        },
    )
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: None)

    lines = _runtime_health_lines()

    stale = _stale_lines(lines)
    assert len(stale) == 1, lines
    assert "recorded state 'running'" in stale[0]


def test_runtime_health_lines_include_fatal_platform_and_startup_reason(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "startup_failed",
            "exit_reason": "telegram conflict",
            "platforms": {
                "telegram": {
                    "state": "fatal",
                    "error_message": "another poller is active",
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: another poller is active" in lines
    assert "⚠ Last startup issue: telegram conflict" in lines


def test_runtime_health_lines_include_discord_lag_watch(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "discord": {
                    "state": "connected",
                    "health": {
                        "status": "online",
                        "latency_ms": 750,
                        "last_heartbeat_ack_age_seconds": 35,
                        "lag_class": "watch",
                    },
                }
            },
        },
    )

    lines = _runtime_health_lines()
    joined = "\n".join(lines)
    assert "discord" in joined
    assert "last heartbeat 35s ago" in joined
    assert "latency 750ms" in joined
    assert "lag watch" in joined


def test_runtime_health_lines_omit_discord_details_when_no_health(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "discord": {"state": "connected"},
            },
        },
    )

    lines = _runtime_health_lines()
    assert all("last heartbeat" not in line for line in lines)
    assert all("lag " not in line for line in lines)


def test_runtime_health_lines_emit_critical_when_offline(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "discord": {
                    "state": "disconnected",
                    "health": {
                        "status": "offline",
                        "latency_ms": None,
                        "last_heartbeat_ack_age_seconds": None,
                        "lag_class": "critical",
                    },
                }
            },
        },
    )

    lines = _runtime_health_lines()
    joined = "\n".join(lines)
    assert "discord" in joined
    assert "offline" in joined
    assert "lag critical" in joined


def test_runtime_health_lines_include_token_pressure(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "token_usage": {
                "last_prompt_tokens": 130_000,
                "context_length": 200_000,
                "pressure_pct": 65,
                "pressure_class": "watch",
                "model": "gpt-5.4",
            },
            "platforms": {},
        },
    )
    lines = _runtime_health_lines()
    joined = "\n".join(lines)
    assert "Token pressure: watch 65% of context on gpt-5.4" in joined


def test_runtime_health_lines_skip_token_pressure_when_incomplete(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "token_usage": {"model": "gpt-5.4"},  # missing pressure_class
            "platforms": {},
        },
    )
    lines = _runtime_health_lines()
    assert all("Token pressure" not in line for line in lines)


def test_runtime_health_lines_render_unknown_pressure_class(monkeypatch):
    """Review-Finding #7: pressure_class='unknown' (context_length missing)
    renders an explicit unknown line rather than silent 'ok'."""
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "token_usage": {
                "last_prompt_tokens": 200_000,
                "context_length": 0,
                "pressure_pct": None,
                "pressure_class": "unknown",
                "model": "gpt-5.4",
            },
            "platforms": {},
        },
    )
    lines = _runtime_health_lines()
    joined = "\n".join(lines)
    assert "Token pressure: unknown (context length not reported) on gpt-5.4" in joined


def test_runtime_health_lines_reject_bool_pressure_pct(monkeypatch):
    """Bool is a subclass of int in Python — a stray True must not render
    as '1% of context'."""
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "token_usage": {
                "pressure_pct": True,
                "pressure_class": "watch",
                "model": "gpt-5.4",
            },
            "platforms": {},
        },
    )
    lines = _runtime_health_lines()
    assert all("Token pressure" not in line for line in lines)


def test_runtime_health_lines_mark_token_pressure_stale(monkeypatch):
    """Review-Finding #14: a token_usage snapshot older than 5min gains a
    '(stale Nm)' / '(stale Nh)' suffix so the operator can tell it's not
    live."""
    from datetime import datetime, timezone, timedelta
    stale_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=8)).isoformat()
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "token_usage": {
                "pressure_pct": 90,
                "pressure_class": "critical",
                "model": "gpt-5.4",
                "updated_at": stale_ts,
            },
            "platforms": {},
        },
    )
    lines = _runtime_health_lines()
    joined = "\n".join(lines)
    assert "(stale 8h)" in joined


def test_runtime_status_running_pid_validates_live_gateway_record(monkeypatch):
    from gateway import status as status_mod

    runtime = {
        "pid": 12345,
        "kind": "hermes-gateway",
        "argv": ["/opt/hermes/hermes_cli/main.py", "gateway", "run", "--replace"],
        "start_time": None,
        "gateway_state": "running",
    }
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: pid == 12345)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: None)
    monkeypatch.setattr(status_mod, "_looks_like_gateway_process", lambda pid: False)

    assert status_mod.get_runtime_status_running_pid(runtime) == 12345


def test_runtime_status_running_pid_rejects_stopped_record(monkeypatch):
    from gateway import status as status_mod

    runtime = {
        "pid": 12345,
        "kind": "hermes-gateway",
        "argv": ["/opt/hermes/hermes_cli/main.py", "gateway", "run", "--replace"],
        "gateway_state": "stopped",
    }
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: True)

    assert status_mod.get_runtime_status_running_pid(runtime) is None
