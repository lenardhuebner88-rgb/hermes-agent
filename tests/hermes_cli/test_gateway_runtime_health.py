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
