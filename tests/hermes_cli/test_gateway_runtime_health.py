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


def test_runtime_health_lines_include_discord_stale_health(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "discord": {
                    "state": "connected",
                    "health": {
                        "status": "stale",
                        "reason": "heartbeat stale",
                        "last_heartbeat_ack_age_seconds": 181,
                    },
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ discord: stale — heartbeat stale (last heartbeat 181s ago)" in lines


def test_runtime_health_lines_include_discord_reconnecting_backoff(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "discord": {
                    "state": "retrying",
                    "health": {
                        "status": "reconnecting",
                        "reason": "failed to reconnect",
                        "reconnect_attempts": 3,
                        "next_retry_seconds": 120,
                    },
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⏳ discord: reconnecting — failed to reconnect (attempt 3, next retry 120s)" in lines
