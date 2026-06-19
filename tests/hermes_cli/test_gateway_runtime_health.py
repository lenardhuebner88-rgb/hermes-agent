from hermes_cli.gateway import _runtime_health_lines


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
