"""Regression coverage for mechanical Board wakeup message relay."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "relay_kanban_escalation_lines.py"
)
_spec = importlib.util.spec_from_file_location("relay_kanban_escalation_lines", _SCRIPT)
assert _spec is not None and _spec.loader is not None
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)


def test_posts_renderer_line_without_changing_any_character():
    rendered_line = (
        "[default] t_marker — Marker | Blocktyp: needs_input | "
        "Grund: A|B\nzweite Zeile … | Operator-Halt: nein"
    )
    posted: list[str] = []

    exit_code = relay.run_wakeup(
        action="post",
        board="default",
        task_id="t_marker",
        render=lambda: [rendered_line],
        send=posted.append,
        write=lambda _text: None,
    )

    assert exit_code == 0
    assert posted == [rendered_line]


def test_renderer_failure_is_posted_as_visible_error_not_quiet_board():
    posted: list[str] = []

    def fail_renderer() -> list[str]:
        raise RuntimeError("MARKER renderer unavailable")

    exit_code = relay.run_wakeup(
        action="inspect",
        board=None,
        task_id=None,
        render=fail_renderer,
        send=posted.append,
        write=lambda _text: None,
    )

    assert exit_code == 1
    assert posted == ["@Dirigent Renderer fehlgeschlagen: MARKER renderer unavailable"]
    assert "ruhig" not in posted[0].lower()


def test_buzz_sender_passes_content_as_one_unchanged_argument():
    rendered_line = "[default] t_marker — Zeichen: | ' \nzweite Zeile"
    commands: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        env = kwargs.get("env")
        assert isinstance(env, dict)
        envs.append(env)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    relay.send_buzz_message(
        rendered_line,
        buzz="/usr/bin/buzz",
        channel=relay.DEFAULT_CHANNEL,
        run=fake_run,
        env={
            "PATH": "/usr/bin",
            "BUZZ_ACP_RECOVERY_PATH": "/tmp/recovery.json",
            "BUZZ_ACP_SOURCE_EVENT_IDS": "a" * 64,
            "KEEP_ME": "yes",
        },
    )

    assert commands == [
        [
            "/usr/bin/buzz",
            "messages",
            "send",
            "--channel",
            relay.DEFAULT_CHANNEL,
            "--content",
            rendered_line,
        ]
    ]
    assert envs[0]["KEEP_ME"] == "yes"
    assert "BUZZ_ACP_RECOVERY_PATH" not in envs[0]
    assert "BUZZ_ACP_SOURCE_EVENT_IDS" not in envs[0]


def test_default_channel_is_board_eskalation_uuid():
    assert relay.DEFAULT_CHANNEL == "7abf1e2a-6629-4ec2-b549-b4833a13a60f"


def test_buzz_sender_rejects_recovered_true_stdout():
    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"accepted":true,"recovered":true,"event_id":"x"}',
            stderr="",
        )

    try:
        relay.send_buzz_message(
            "line",
            buzz="/usr/bin/buzz",
            channel=relay.DEFAULT_CHANNEL,
            run=fake_run,
            env={"PATH": "/usr/bin"},
        )
    except RuntimeError as exc:
        assert "recovered" in str(exc).lower()
    else:
        raise AssertionError("expected RuntimeError for recovered:true")


def test_process_safe_line_survives_real_subprocess_argv():
    """Layer that 6c mutates: argv must accept the normalized line."""
    line = (
        "[default] t_proc — `nul\ufffdtitle` | Blocktyp: needs_input | "
        "Grund: `reason\ufffdand\ufffdsurrogate` | Operator-Halt: nein"
    )
    # Same shape as the relay: one argv element into a real process.
    result = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", line],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "\ufffd" in result.stdout
