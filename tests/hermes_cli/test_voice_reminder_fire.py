"""Tests for scripts/voice_reminder_fire.py — the standalone reminder firer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from scripts.voice_reminder_fire import main


def _write_payload(reminders_dir: Path, text: str) -> Path:
    reminders_dir.mkdir(parents=True, exist_ok=True)
    payload_path = reminders_dir / "reminder-1.json"
    payload_path.write_text(
        json.dumps({"text": text, "created_at": "2026-07-10T12:00:00+00:00"}),
        encoding="utf-8",
    )
    return payload_path


def test_main_success_sends_discord_message_and_unlinks_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reminders_dir = tmp_path / "cache" / "voice-web" / "reminders"
    payload_path = _write_payload(reminders_dir, "Wäsche umschichten")

    with patch(
        "tools.send_message_tool.send_message_tool",
        return_value=json.dumps({"success": True}),
    ) as sender:
        exit_code = main([str(payload_path)])

    assert exit_code == 0
    assert not payload_path.exists()
    sent_args = sender.call_args.args[0]
    assert sent_args["target"] == "discord"
    assert "⏰ Erinnerung:" in sent_args["message"]
    assert "Wäsche umschichten" in sent_args["message"]


def test_main_failure_keeps_payload_and_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reminders_dir = tmp_path / "cache" / "voice-web" / "reminders"
    payload_path = _write_payload(reminders_dir, "Wäsche umschichten")

    with patch(
        "tools.send_message_tool.send_message_tool",
        return_value=json.dumps({"error": "no home channel set"}),
    ):
        exit_code = main([str(payload_path)])

    assert exit_code != 0
    assert payload_path.exists()


def test_main_refuses_payload_outside_reminders_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir(parents=True, exist_ok=True)
    payload_path = _write_payload(outside_dir, "Wäsche umschichten")

    with patch("tools.send_message_tool.send_message_tool") as sender:
        exit_code = main([str(payload_path)])

    assert exit_code != 0
    assert payload_path.exists()
    sender.assert_not_called()


# ---------------------------------------------------------------------------
# Exact exit-code contract (systemd-run + callers key off these codes)
# ---------------------------------------------------------------------------

def test_main_wrong_argc_is_usage_error_2(tmp_path, monkeypatch):
    """No/too many arguments is a USAGE error (2), not a runtime failure."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert main([]) == 2
    assert main(["a", "b"]) == 2


def test_main_outside_reminders_dir_is_usage_error_2(tmp_path, monkeypatch):
    """A payload outside the reminders dir is a refusal (2) — distinct
    from a runtime failure so a mis-scheduled unit doesn't retry-loop."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir(parents=True, exist_ok=True)
    payload_path = _write_payload(outside_dir, "x")
    with patch("tools.send_message_tool.send_message_tool"):
        assert main([str(payload_path)]) == 2


def test_main_unreadable_or_empty_payload_is_runtime_failure_1(
    tmp_path, monkeypatch
):
    """A missing file or a payload without text is a runtime failure (1),
    not a usage error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reminders_dir = tmp_path / "cache" / "voice-web" / "reminders"
    reminders_dir.mkdir(parents=True)

    missing = reminders_dir / "missing.json"
    with patch("tools.send_message_tool.send_message_tool"):
        assert main([str(missing)]) == 1

    empty = reminders_dir / "empty.json"
    empty.write_text(json.dumps({"text": "  "}), encoding="utf-8")
    with patch("tools.send_message_tool.send_message_tool"):
        assert main([str(empty)]) == 1


def test_main_delivery_failure_is_runtime_failure_1(tmp_path, monkeypatch):
    """A rejected Discord delivery is a runtime failure (1) — the payload
    stays on disk for a retry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reminders_dir = tmp_path / "cache" / "voice-web" / "reminders"
    payload_path = _write_payload(reminders_dir, "x")
    with patch(
        "tools.send_message_tool.send_message_tool",
        return_value=json.dumps({"error": "no home channel set"}),
    ):
        assert main([str(payload_path)]) == 1
