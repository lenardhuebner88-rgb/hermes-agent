#!/usr/bin/env python3
"""Relay renderer-owned Kanban escalation lines without model transcription."""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

DEFAULT_CHANNEL = "board-eskalation"


def render_or_report(
    *,
    render: Callable[[], Sequence[str]],
    send: Callable[[str], None],
) -> Sequence[str] | None:
    """Return renderer-owned lines or post a visible error and stop the wakeup."""
    try:
        return render()
    except Exception as exc:
        send(f"@Dirigent Renderer fehlgeschlagen: {exc}")
        return None


def post_task_line(
    *,
    board: str,
    task_id: str,
    render: Callable[[], Sequence[str]],
    send: Callable[[str], None],
) -> str:
    """Render and post the one logical line owned by ``board`` and ``task_id``."""
    prefix = f"[{board}] {task_id} — "
    matches = [line for line in render() if line.startswith(prefix)]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one renderer line for {board}/{task_id}, got {len(matches)}"
        )
    line = matches[0]
    send(line)
    return line


def run_wakeup(
    *,
    action: str,
    board: str | None,
    task_id: str | None,
    render: Callable[[], Sequence[str]],
    send: Callable[[str], None],
    write: Callable[[str], object],
) -> int:
    """Inspect renderer output or mechanically post one selected logical line."""
    lines = render_or_report(render=render, send=send)
    if lines is None:
        return 1

    if action == "inspect":
        for line in lines:
            write(f"{line}\n")
        return 0

    if action != "post":
        raise ValueError(f"unknown action: {action}")
    if board is None or task_id is None:
        raise ValueError("post requires board and task_id")

    try:
        post_task_line(
            board=board,
            task_id=task_id,
            render=lambda: lines,
            send=send,
        )
    except RuntimeError as exc:
        send(f"@Dirigent Renderer-Auswahl fehlgeschlagen: {exc}")
        return 1
    return 0


def send_buzz_message(
    content: str,
    *,
    buzz: str,
    channel: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Send ``content`` as one argv value so no shell or model can rewrite it."""
    command = [
        buzz,
        "messages",
        "send",
        "--channel",
        channel,
        "--content",
        content,
    ]
    result = run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"buzz message send failed (Exit {result.returncode}){suffix}")


def _load_renderer(script_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "render_kanban_escalation_lines", script_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load renderer: {script_path}")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    return renderer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or relay renderer-owned Kanban escalation lines."
    )
    parser.add_argument(
        "--renderer",
        type=Path,
        default=Path(__file__).with_name("render_kanban_escalation_lines.py"),
        help="path to the read-only escalation renderer",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes home containing the configured Kanban boards",
    )
    parser.add_argument("--buzz", default="buzz", help="Buzz CLI executable")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("inspect", help="print renderer output for Board-agent review")
    post = actions.add_parser("post", help="post one renderer-owned card line")
    post.add_argument("--board", required=True)
    post.add_argument("--task-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    def render() -> Sequence[str]:
        renderer = _load_renderer(args.renderer)
        return renderer.render_all_boards(args.hermes_home)

    def send(content: str) -> None:
        send_buzz_message(content, buzz=args.buzz, channel=args.channel)

    try:
        return run_wakeup(
            action=args.action,
            board=getattr(args, "board", None),
            task_id=getattr(args, "task_id", None),
            render=render,
            send=send,
            write=sys.stdout.write,
        )
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
