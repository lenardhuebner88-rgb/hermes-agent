"""Regression coverage for mechanical Board wakeup message relay."""
from __future__ import annotations

import importlib.util
import subprocess
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

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    relay.send_buzz_message(
        rendered_line,
        buzz="/usr/bin/buzz",
        channel="board-eskalation",
        run=fake_run,
    )

    assert commands == [
        [
            "/usr/bin/buzz",
            "messages",
            "send",
            "--channel",
            "board-eskalation",
            "--content",
            rendered_line,
        ]
    ]
