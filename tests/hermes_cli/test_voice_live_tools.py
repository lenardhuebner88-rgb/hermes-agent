from unittest.mock import MagicMock, patch

import pytest

from tools.voice_live_tools import FUNCTION_DECLARATIONS, VoiceToolExecutor


def _proc(stdout: str, rc: int = 0):
    process = MagicMock()
    process.stdout = stdout
    process.returncode = rc
    return process


@pytest.mark.asyncio
async def test_list_terminals_parses_tmux_output():
    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.subprocess.run",
        return_value=_proc("main|1\nkanban-w1|0\n"),
    ):
        result = await executor.execute("list_terminals", {})

    assert result["terminals"] == [
        {"name": "main", "attached": True},
        {"name": "kanban-w1", "attached": False},
    ]


@pytest.mark.asyncio
async def test_send_to_terminal_uses_literal_send_keys():
    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.subprocess.run", return_value=_proc("")
    ) as run:
        result = await executor.execute(
            "send_to_terminal", {"session": "main", "command": "git status"}
        )

    sent = run.call_args_list[0].args[0]
    assert sent[:4] == ["tmux", "send-keys", "-t", "main"] and "-l" in sent
    assert result["ok"] is True


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
