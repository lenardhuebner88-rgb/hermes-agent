import subprocess
from unittest.mock import MagicMock, patch

import pytest

from tools.voice_live_tools import FUNCTION_DECLARATIONS, VoiceToolExecutor


def _proc(stdout: str, rc: int = 0, stderr: str = ""):
    process = MagicMock()
    process.stdout = stdout
    process.returncode = rc
    process.stderr = stderr
    return process


@pytest.mark.asyncio
async def test_list_terminals_parses_tmux_output():
    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.subprocess.run",
        return_value=_proc("main|1\nwork|2\nkanban-w1|0\nbroken|unknown\n"),
    ):
        result = await executor.execute("list_terminals", {})

    assert result["terminals"] == [
        {"name": "main", "attached": True},
        {"name": "work", "attached": True},
        {"name": "kanban-w1", "attached": False},
        {"name": "broken", "attached": False},
    ]


@pytest.mark.asyncio
async def test_send_to_terminal_uses_literal_send_keys():
    executor = VoiceToolExecutor(delegate=None)
    with patch("tools.voice_live_tools.subprocess.run", return_value=_proc("")) as run:
        result = await executor.execute(
            "send_to_terminal", {"session": "main", "command": "-literal command"}
        )

    sent = run.call_args_list[0].args[0]
    assert sent == [
        "tmux",
        "send-keys",
        "-t",
        "main",
        "-l",
        "--",
        "-literal command",
    ]
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_tmux_subprocess_is_dispatched_via_to_thread():
    async def fake_to_thread(function, *args, **kwargs):
        assert function is subprocess.run
        return _proc("")

    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.asyncio.to_thread", side_effect=fake_to_thread
    ) as to_thread:
        result = await executor.execute("list_terminals", {})

    to_thread.assert_awaited_once()
    assert result == {"terminals": []}


@pytest.mark.parametrize(("requested", "expected"), [(-10, "-1"), (500, "-200")])
@pytest.mark.asyncio
async def test_read_terminal_clamps_line_count(requested, expected):
    executor = VoiceToolExecutor(delegate=None)
    with patch("tools.voice_live_tools.subprocess.run", return_value=_proc("")) as run:
        await executor.execute(
            "read_terminal", {"session": "work:main", "lines": requested}
        )

    assert run.call_args.args[0][-1] == expected


@pytest.mark.asyncio
async def test_send_failure_returns_structured_error_without_enter():
    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.subprocess.run",
        return_value=_proc("", rc=7, stderr="can't find pane"),
    ) as run:
        result = await executor.execute(
            "send_to_terminal", {"session": "work", "command": "status"}
        )

    assert result["error"] == {
        "code": "tmux_failed",
        "message": "tmux-Aktion 'send_to_terminal' ist fehlgeschlagen.",
        "action": "send_to_terminal",
        "returncode": 7,
        "stderr": "can't find pane",
    }
    assert run.call_count == 1


@pytest.mark.asyncio
async def test_delegate_calls_injected_callback():
    async def fake_delegate(prompt):
        return "erledigt: " + prompt

    executor = VoiceToolExecutor(delegate=fake_delegate)
    result = await executor.execute(
        "delegate_to_hermes", {"prompt": "räum die Queue auf"}
    )

    assert result["result"].startswith("erledigt")


def test_function_declarations_cover_all_tools():
    names = {declaration["name"] for declaration in FUNCTION_DECLARATIONS}
    assert names == {
        "list_terminals",
        "read_terminal",
        "send_to_terminal",
        "delegate_to_hermes",
    }
