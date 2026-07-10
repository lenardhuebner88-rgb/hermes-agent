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
