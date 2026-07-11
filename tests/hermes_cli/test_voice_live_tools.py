import asyncio
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.voice_live_tools import (
    FUNCTION_DECLARATIONS,
    NON_BLOCKING_TOOLS,
    VOICE_FRAME_ARG,
    VoiceToolExecutor,
)

FIXTURE_VIDEO = Path(__file__).parent / "fixtures" / "vision_marker.jpg"


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


@pytest.mark.asyncio
async def test_delegate_with_internal_frame_uses_image_callback_without_consuming_prompt():
    observed = {}

    async def fake_delegate(prompt):
        raise AssertionError(f"plain delegate unexpectedly called: {prompt}")

    async def fake_delegate_with_image(prompt, image):
        observed["prompt"] = prompt
        observed["image"] = image
        return "Bild erhalten"

    executor = VoiceToolExecutor(
        delegate=fake_delegate,
        delegate_with_image=fake_delegate_with_image,
    )
    frame = b"\xff\xd8delegation-frame\xff\xd9"
    result = await executor.execute(
        "delegate_to_hermes",
        {"prompt": "prüfe den sichtbaren Fehler", VOICE_FRAME_ARG: frame},
    )

    assert result == {"result": "Bild erhalten"}
    assert observed == {
        "prompt": "prüfe den sichtbaren Fehler",
        "image": frame,
    }


@pytest.mark.asyncio
async def test_watch_tools_call_injected_callbacks_and_stop_is_idempotent():
    state = {"instruction": None, "watching": False}

    def watch_view(instruction):
        state.update(instruction=instruction, watching=True)
        return {"watching": True, "instruction": instruction}

    def stop_watching():
        was_watching = state["watching"]
        state["watching"] = False
        return {"watching": False, "was_watching": was_watching}

    executor = VoiceToolExecutor(
        delegate=None,
        watch_view=watch_view,
        stop_watching=stop_watching,
    )

    started = await executor.execute(
        "watch_view", {"instruction": "Prüfe den Build"}
    )
    stopped = await executor.execute("stop_watching", {})
    stopped_again = await executor.execute("stop_watching", {})

    assert started == {"watching": True, "instruction": "Prüfe den Build"}
    assert stopped == {"watching": False, "was_watching": True}
    assert stopped_again == {"watching": False, "was_watching": False}


@pytest.mark.asyncio
async def test_watch_view_rejects_missing_instruction_and_unavailable_callback():
    executor = VoiceToolExecutor(delegate=None)

    missing = await executor.execute("watch_view", {})
    unavailable = await executor.execute(
        "watch_view", {"instruction": "Prüfe den Build"}
    )

    assert missing["error"]["code"] == "invalid_arguments"
    assert unavailable["error"]["code"] == "watch_unavailable"


def _fake_generate_content_response(
    text, *, prompt_tokens=120, candidate_tokens=14, thoughts_tokens=None
):
    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.candidates_token_count = candidate_tokens
    usage.thoughts_token_count = thoughts_tokens
    response = MagicMock()
    response.text = text
    response.usage_metadata = usage
    return response


@pytest.mark.asyncio
async def test_look_closely_sends_real_jpeg_bytes_and_reports_usage():
    """Pflicht-Test gegen das echte Datenformat: ein reales JPEG-Fixture
    (nicht "abc123") muss byte-exakt + mit korrektem mime_type bei
    generate_content ankommen, und die Nutzung wird gemeldet."""

    real_jpeg = FIXTURE_VIDEO.read_bytes()
    assert real_jpeg.startswith(b"\xff\xd8")  # a real JPEG, not a synthetic string

    async def fake_request_frame():
        return real_jpeg

    reported = {}

    def report_usage(input_tokens, output_tokens, complete):
        reported["input"] = input_tokens
        reported["output"] = output_tokens
        reported["complete"] = complete

    generate_content = AsyncMock(
        return_value=_fake_generate_content_response("Ich sehe ein Testbild.")
    )
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = generate_content

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        look_model="gemini-3.1-flash-lite",
        gemini_api_key="test-key",
        report_look_usage=report_usage,
    )

    with patch(
        "tools.voice_live_tools.genai.Client", return_value=fake_client
    ) as client_ctor:
        result = await executor.execute(
            "look_closely", {"question": "Was steht auf dem Bild?"}
        )

    assert result == {"answer": "Ich sehe ein Testbild."}
    client_ctor.assert_called_once_with(api_key="test-key")
    generate_content.assert_awaited_once()
    call_kwargs = generate_content.await_args.kwargs
    assert call_kwargs["model"] == "gemini-3.1-flash-lite"
    parts = call_kwargs["contents"].parts
    assert parts[0].inline_data.data == real_jpeg
    assert parts[0].inline_data.mime_type == "image/jpeg"
    assert parts[1].text == "Was steht auf dem Bild?"
    # Anti-injection: visible text/instructions inside the image must never
    # be followed — the system_instruction says so explicitly.
    assert (
        "UNVERTRAUENSWÜRDIGE"
        in call_kwargs["config"].system_instruction
    )
    assert reported == {"input": 120, "output": 14, "complete": True}


@pytest.mark.asyncio
async def test_look_closely_folds_thoughts_tokens_into_output_tokens():
    """flash-lite can think; thinking tokens are billed as output, same as
    candidates tokens — losing them would under-report cost."""

    real_jpeg = FIXTURE_VIDEO.read_bytes()

    async def fake_request_frame():
        return real_jpeg

    reported = {}

    def report_usage(input_tokens, output_tokens, complete):
        reported["input"] = input_tokens
        reported["output"] = output_tokens
        reported["complete"] = complete

    generate_content = AsyncMock(
        return_value=_fake_generate_content_response(
            "Antwort.", prompt_tokens=100, candidate_tokens=20, thoughts_tokens=35
        )
    )
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = generate_content

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        gemini_api_key="test-key",
        report_look_usage=report_usage,
    )

    with patch("tools.voice_live_tools.genai.Client", return_value=fake_client):
        await executor.execute("look_closely", {"question": "Was ist das?"})

    assert reported == {"input": 100, "output": 55, "complete": True}


@pytest.mark.asyncio
async def test_look_closely_missing_usage_metadata_reports_incomplete():
    """No usage_metadata at all must still count the call, but flagged
    incomplete rather than silently reporting 0 tokens as if that were the
    real (and free) cost."""

    real_jpeg = FIXTURE_VIDEO.read_bytes()

    async def fake_request_frame():
        return real_jpeg

    reported = {}

    def report_usage(input_tokens, output_tokens, complete):
        reported["input"] = input_tokens
        reported["output"] = output_tokens
        reported["complete"] = complete

    response = MagicMock()
    response.text = "Antwort."
    response.usage_metadata = None
    generate_content = AsyncMock(return_value=response)
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = generate_content

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        gemini_api_key="test-key",
        report_look_usage=report_usage,
    )

    with patch("tools.voice_live_tools.genai.Client", return_value=fake_client):
        await executor.execute("look_closely", {"question": "Was ist das?"})

    assert reported == {"input": 0, "output": 0, "complete": False}


@pytest.mark.asyncio
async def test_look_closely_missing_question_is_invalid_arguments():
    executor = VoiceToolExecutor(delegate=None, request_frame=None)
    result = await executor.execute("look_closely", {"question": "   "})
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_look_closely_unavailable_without_callback_or_key():
    executor = VoiceToolExecutor(delegate=None)
    result = await executor.execute(
        "look_closely", {"question": "Was ist das?"}
    )
    assert result["error"]["code"] == "look_unavailable"


@pytest.mark.asyncio
async def test_look_closely_no_frame_returns_structured_error():
    async def fake_request_frame():
        return None

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        gemini_api_key="test-key",
    )
    result = await executor.execute(
        "look_closely", {"question": "Was ist das?"}
    )
    assert result["error"]["code"] == "no_frame"


@pytest.mark.asyncio
async def test_look_closely_timeout_returns_structured_error():
    real_jpeg = FIXTURE_VIDEO.read_bytes()

    async def fake_request_frame():
        return real_jpeg

    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(10)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = hang_forever

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        gemini_api_key="test-key",
    )

    with (
        patch("tools.voice_live_tools.genai.Client", return_value=fake_client),
        patch("tools.voice_live_tools._LOOK_CLOSELY_TIMEOUT_SECONDS", 0.01),
    ):
        result = await executor.execute(
            "look_closely", {"question": "Was ist das?"}
        )

    assert result["error"]["code"] == "look_timeout"


@pytest.mark.asyncio
async def test_look_closely_timeout_still_reports_usage_as_incomplete():
    """Codex-R2 finding #3: a timeout after the call started must not leave
    the cost estimate silently marked complete — report (0, 0, False)."""

    real_jpeg = FIXTURE_VIDEO.read_bytes()

    async def fake_request_frame():
        return real_jpeg

    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(10)

    reported = {}

    def report_usage(input_tokens, output_tokens, complete):
        reported["input"] = input_tokens
        reported["output"] = output_tokens
        reported["complete"] = complete

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = hang_forever

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        gemini_api_key="test-key",
        report_look_usage=report_usage,
    )

    with (
        patch("tools.voice_live_tools.genai.Client", return_value=fake_client),
        patch("tools.voice_live_tools._LOOK_CLOSELY_TIMEOUT_SECONDS", 0.01),
    ):
        result = await executor.execute(
            "look_closely", {"question": "Was ist das?"}
        )

    assert result["error"]["code"] == "look_timeout"
    assert reported == {"input": 0, "output": 0, "complete": False}


@pytest.mark.asyncio
async def test_look_closely_generate_content_failure_still_reports_usage():
    """Codex-R2 finding #3, generic-exception branch: same requirement as
    the timeout path — a failed call must not vanish from the estimate."""

    real_jpeg = FIXTURE_VIDEO.read_bytes()

    async def fake_request_frame():
        return real_jpeg

    reported = {}

    def report_usage(input_tokens, output_tokens, complete):
        reported["input"] = input_tokens
        reported["output"] = output_tokens
        reported["complete"] = complete

    generate_content = AsyncMock(side_effect=RuntimeError("upstream exploded"))
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = generate_content

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        gemini_api_key="test-key",
        report_look_usage=report_usage,
    )

    with patch("tools.voice_live_tools.genai.Client", return_value=fake_client):
        result = await executor.execute(
            "look_closely", {"question": "Was ist das?"}
        )

    assert result["error"]["code"] == "look_failed"
    assert reported == {"input": 0, "output": 0, "complete": False}


@pytest.mark.asyncio
async def test_look_closely_negative_raw_token_field_clamped_and_incomplete():
    """Codex-R2 finding #4: a negative raw field (e.g. a buggy/adversarial
    thoughts_token_count) must be clamped to 0 for its own field before
    summing — never let it eat into candidates_token_count — and the call
    must be flagged incomplete rather than silently under-reporting."""

    real_jpeg = FIXTURE_VIDEO.read_bytes()

    async def fake_request_frame():
        return real_jpeg

    reported = {}

    def report_usage(input_tokens, output_tokens, complete):
        reported["input"] = input_tokens
        reported["output"] = output_tokens
        reported["complete"] = complete

    generate_content = AsyncMock(
        return_value=_fake_generate_content_response(
            "Antwort.", prompt_tokens=100, candidate_tokens=20, thoughts_tokens=-999
        )
    )
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = generate_content

    executor = VoiceToolExecutor(
        delegate=None,
        request_frame=fake_request_frame,
        gemini_api_key="test-key",
        report_look_usage=report_usage,
    )

    with patch("tools.voice_live_tools.genai.Client", return_value=fake_client):
        await executor.execute("look_closely", {"question": "Was ist das?"})

    # thoughts_token_count=-999 must not subtract from candidates (20) — the
    # negative field is clamped to 0 on its own before the sum, not summed
    # first and clamped after (which would still report 0, masking the bug).
    assert reported == {"input": 100, "output": 20, "complete": False}


def test_function_declarations_cover_all_tools():
    names = {declaration["name"] for declaration in FUNCTION_DECLARATIONS}
    assert names == {
        "phone_action",
        "list_terminals",
        "read_terminal",
        "send_to_terminal",
        "delegate_to_hermes",
        "watch_view",
        "stop_watching",
        "look_closely",
        "send_discord_message",
        "create_kanban_task",
        "hermes_status",
        "recall_memory",
        "schedule_reminder",
    }


def test_delegate_declaration_is_non_blocking():
    delegate = next(
        decl for decl in FUNCTION_DECLARATIONS if decl["name"] == "delegate_to_hermes"
    )
    assert delegate["behavior"] == "NON_BLOCKING"
    other_names = {
        decl["name"] for decl in FUNCTION_DECLARATIONS if decl["name"] != "delegate_to_hermes"
    }
    for name in other_names:
        decl = next(d for d in FUNCTION_DECLARATIONS if d["name"] == name)
        assert "behavior" not in decl


def test_function_declarations_validate_against_real_sdk():
    from google.genai import types

    tool = types.Tool(function_declarations=FUNCTION_DECLARATIONS)

    names = {decl.name for decl in tool.function_declarations}
    assert names == {decl["name"] for decl in FUNCTION_DECLARATIONS}
    delegate = next(
        decl for decl in tool.function_declarations if decl.name == "delegate_to_hermes"
    )
    assert delegate.behavior == types.Behavior.NON_BLOCKING


def test_non_blocking_tools_constant_matches_declarations():
    assert NON_BLOCKING_TOOLS == frozenset({"delegate_to_hermes", "phone_action"})


def test_is_non_blocking_flags_only_delegate_to_hermes():
    executor = VoiceToolExecutor(delegate=None)
    assert executor.is_non_blocking("delegate_to_hermes") is True
    for name in (
        "list_terminals",
        "read_terminal",
        "send_to_terminal",
        "send_discord_message",
        "create_kanban_task",
        "hermes_status",
        "recall_memory",
        "schedule_reminder",
        "watch_view",
        "stop_watching",
        "look_closely",
    ):
        assert executor.is_non_blocking(name) is False


@pytest.mark.asyncio
async def test_send_discord_message_success():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch("tools.voice_live_tools._ensure_hermes_env") as seeder,
        patch(
            "tools.send_message_tool.send_message_tool",
            return_value=json.dumps({"success": True}),
        ) as sender,
    ):
        result = await executor.execute(
            "send_discord_message", {"text": "hallo piet"}
        )

    assert result == {"ok": True}
    seeder.assert_called_once_with()
    sender.assert_called_once_with({"target": "discord", "message": "hallo piet"})


def test_ensure_hermes_env_seeds_without_clobbering(monkeypatch):
    from tools.voice_live_tools import _ensure_hermes_env

    monkeypatch.setenv("VOICE_ENV_EXISTING", "keep-me")
    monkeypatch.delenv("VOICE_ENV_MISSING", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_env",
        lambda: {
            "VOICE_ENV_EXISTING": "overwrite-attempt",
            "VOICE_ENV_MISSING": "seeded",
            "VOICE_ENV_NONE": None,
        },
    )

    _ensure_hermes_env()

    assert os.environ["VOICE_ENV_EXISTING"] == "keep-me"
    assert os.environ["VOICE_ENV_MISSING"] == "seeded"
    assert "VOICE_ENV_NONE" not in os.environ
    monkeypatch.delenv("VOICE_ENV_MISSING", raising=False)


@pytest.mark.asyncio
async def test_send_discord_message_missing_text_is_invalid_arguments():
    executor = VoiceToolExecutor(delegate=None)
    result = await executor.execute("send_discord_message", {"text": "  "})
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_send_discord_message_failure_returns_structured_error():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch("tools.voice_live_tools._ensure_hermes_env"),
        patch(
            "tools.send_message_tool.send_message_tool",
            return_value=json.dumps({"error": "no home channel set"}),
        ),
    ):
        result = await executor.execute(
            "send_discord_message", {"text": "hallo piet"}
        )

    assert result["error"]["code"] == "discord_send_failed"
    assert "no home channel set" in result["error"]["message"]


@pytest.mark.asyncio
async def test_send_discord_message_exception_returns_structured_error():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch("tools.voice_live_tools._ensure_hermes_env"),
        patch(
            "tools.send_message_tool.send_message_tool",
            side_effect=RuntimeError("gateway down"),
        ),
    ):
        result = await executor.execute(
            "send_discord_message", {"text": "hallo piet"}
        )

    assert result["error"]["code"] == "discord_send_failed"
    assert "gateway down" in result["error"]["detail"]


@pytest.mark.asyncio
async def test_send_discord_message_truncates_long_text():
    executor = VoiceToolExecutor(delegate=None)
    long_text = "x" * 2000
    with (
        patch("tools.voice_live_tools._ensure_hermes_env"),
        patch(
            "tools.send_message_tool.send_message_tool",
            return_value=json.dumps({"success": True}),
        ) as sender,
    ):
        result = await executor.execute("send_discord_message", {"text": long_text})

    assert result == {"ok": True, "truncated": True}
    sent_args = sender.call_args.args[0]
    assert len(sent_args["message"]) == 1800


@pytest.mark.asyncio
async def test_create_kanban_task_success_with_description_none_passthrough():
    executor = VoiceToolExecutor(delegate=None)
    closed = []

    class FakeConn:
        def close(self):
            closed.append(True)

    fake_conn = FakeConn()
    calls = {}

    def fake_connect():
        return fake_conn

    def fake_create_task(conn, *, title, body, created_by):
        calls["conn"] = conn
        calls["title"] = title
        calls["body"] = body
        calls["created_by"] = created_by
        return "t_abc123"

    with patch("hermes_cli.kanban_db.connect", fake_connect), patch(
        "hermes_cli.kanban_db.create_task", fake_create_task
    ):
        result = await executor.execute(
            "create_kanban_task", {"title": "Wäsche waschen"}
        )

    assert result == {"task_id": "t_abc123", "title": "Wäsche waschen"}
    assert calls == {
        "conn": fake_conn,
        "title": "Wäsche waschen",
        "body": None,
        "created_by": "voice",
    }
    assert closed == [True]


@pytest.mark.asyncio
async def test_create_kanban_task_passes_through_description():
    executor = VoiceToolExecutor(delegate=None)
    calls = {}

    def fake_connect():
        return MagicMock()

    def fake_create_task(conn, *, title, body, created_by):
        calls["body"] = body
        return "t_xyz789"

    with patch("hermes_cli.kanban_db.connect", fake_connect), patch(
        "hermes_cli.kanban_db.create_task", fake_create_task
    ):
        result = await executor.execute(
            "create_kanban_task",
            {"title": "Wäsche waschen", "description": "vor 18 Uhr"},
        )

    assert result["task_id"] == "t_xyz789"
    assert calls["body"] == "vor 18 Uhr"


@pytest.mark.asyncio
async def test_create_kanban_task_missing_title_is_invalid_arguments():
    executor = VoiceToolExecutor(delegate=None)
    result = await executor.execute("create_kanban_task", {})
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_create_kanban_task_failure_returns_structured_error():
    executor = VoiceToolExecutor(delegate=None)

    def fake_connect():
        raise RuntimeError("db locked")

    with patch("hermes_cli.kanban_db.connect", fake_connect):
        result = await executor.execute("create_kanban_task", {"title": "x"})

    assert result["error"]["code"] == "kanban_task_failed"
    assert "db locked" in result["error"]["detail"]


@pytest.mark.asyncio
async def test_hermes_status_compacts_board_stats_and_worker_count():
    executor = VoiceToolExecutor(delegate=None)

    class FakeCursor:
        def fetchone(self):
            return {"n": 2}

    class FakeConn:
        def __init__(self):
            self.closed = False

        def execute(self, _query):
            return FakeCursor()

        def close(self):
            self.closed = True

    fake_conn = FakeConn()

    def fake_connect():
        return fake_conn

    def fake_board_stats(conn):
        assert conn is fake_conn
        return {
            "by_status": {"ready": 3, "running": 1},
            "by_assignee": {},
            "completed_last_24h": 5,
            "completed_last_7d": 12,
        }

    with patch("hermes_cli.kanban_db.connect", fake_connect), patch(
        "hermes_cli.kanban_db.board_stats", fake_board_stats
    ):
        result = await executor.execute("hermes_status", {})

    assert result == {
        "aufgaben_nach_status": {"ready": 3, "running": 1},
        "aktive_worker": 2,
        "abgeschlossen_24h": 5,
    }
    assert fake_conn.closed is True


@pytest.mark.asyncio
async def test_hermes_status_drops_empty_status_breakdown():
    executor = VoiceToolExecutor(delegate=None)

    class FakeCursor:
        def fetchone(self):
            return {"n": 0}

    class FakeConn:
        def execute(self, _query):
            return FakeCursor()

        def close(self):
            pass

    def fake_connect():
        return FakeConn()

    def fake_board_stats(conn):
        return {"by_status": {}, "completed_last_24h": 0}

    with patch("hermes_cli.kanban_db.connect", fake_connect), patch(
        "hermes_cli.kanban_db.board_stats", fake_board_stats
    ):
        result = await executor.execute("hermes_status", {})

    assert result == {"aktive_worker": 0, "abgeschlossen_24h": 0}


@pytest.mark.asyncio
async def test_hermes_status_failure_returns_structured_error():
    executor = VoiceToolExecutor(delegate=None)

    def fake_connect():
        raise RuntimeError("db locked")

    with patch("hermes_cli.kanban_db.connect", fake_connect):
        result = await executor.execute("hermes_status", {})

    assert result["error"]["code"] == "status_unavailable"


@pytest.mark.asyncio
async def test_schedule_reminder_writes_payload_and_invokes_systemd_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.subprocess.run",
        return_value=_proc("", rc=0),
    ) as run:
        result = await executor.execute(
            "schedule_reminder", {"minutes": 30, "text": "Wäsche umschichten"}
        )

    assert result == {"ok": True, "minuten": 30}
    command = run.call_args.args[0]
    assert command[0] == "systemd-run"
    assert "--user" in command
    assert "--collect" in command
    assert "--on-active=30min" in command
    assert any(part.startswith("--unit=hermes-voice-reminder-") for part in command)
    # The transient unit starts from the user-manager environment, so the
    # profile-selected hermes home must be pinned explicitly — otherwise the
    # fire script resolves the default home and rejects the payload path.
    assert f"--setenv=HERMES_HOME={tmp_path}" in command
    assert command[-2].endswith("voice_reminder_fire.py")

    payload_path = Path(command[-1])
    assert payload_path.parent == tmp_path / "cache" / "voice-web" / "reminders"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["text"] == "Wäsche umschichten"
    assert "created_at" in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 1441])
async def test_schedule_reminder_rejects_out_of_range_minutes(minutes, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    executor = VoiceToolExecutor(delegate=None)
    with patch("tools.voice_live_tools.subprocess.run") as run:
        result = await executor.execute(
            "schedule_reminder", {"minutes": minutes, "text": "Test"}
        )

    assert result["error"]["code"] == "invalid_arguments"
    run.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_reminder_missing_text_is_invalid_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    executor = VoiceToolExecutor(delegate=None)
    result = await executor.execute("schedule_reminder", {"minutes": 5, "text": " "})
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_schedule_reminder_missing_systemd_run_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.subprocess.run",
        side_effect=FileNotFoundError("systemd-run"),
    ):
        result = await executor.execute(
            "schedule_reminder", {"minutes": 5, "text": "Test"}
        )

    assert result["error"]["code"] == "systemd_unavailable"


@pytest.mark.asyncio
async def test_schedule_reminder_nonzero_exit_returns_structured_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    executor = VoiceToolExecutor(delegate=None)
    with patch(
        "tools.voice_live_tools.subprocess.run",
        return_value=_proc("", rc=1, stderr="Failed to start transient unit"),
    ):
        result = await executor.execute(
            "schedule_reminder", {"minutes": 5, "text": "Test"}
        )

    assert result["error"]["code"] == "reminder_schedule_failed"
    assert "Failed to start transient unit" in result["error"]["stderr"]


# ---------------------------------------------------------------------------
# recall_memory — hermes-memsearch-recall CLI wrapper
# ---------------------------------------------------------------------------

# Real output from a live `hermes-memsearch-recall -k 5 "voice plan g dictate
# ime"` probe run (2026-07-11), truncated markers included exactly as the CLI
# emits them — this is the actual wire format the tool has to parse/truncate,
# not a synthetic guess.
_REAL_MEMSEARCH_OUTPUT = """--- Result 1 (score: 0.9919) ---
Source: /home/piet/.memsearch/shared/memory/2026-07-10.md
Heading: 14:44
- Claude Code präsentierte Option A (schnell): Wispr Flow's Dauer-Zuhören/Wake-Word in den Einstellungen deaktivieren, sodass Wispr das Mikro nur während aktiven Diktierens hält und danach freigibt; zusätzlich die Fehlermeldung in Hermes Voice verbessern mit Auto-Retry-Knopf.
  ... [truncated, run 'memsearch expand 08fd02f901f6c7a9' for full content]

--- Result 2 (score: 0.5000) ---
Source: /home/piet/.memsearch/shared/memory/2026-07-10.md
Heading: 22:45
### 22:45
- User forderte auf, Voice Plan G nach einem Host-Reboot vom letzten belegten Stand fortzusetzen.
  ... [truncated, run 'memsearch expand 1e9164b0e3f16d78' for full content]
"""


@pytest.mark.asyncio
async def test_recall_memory_success_returns_real_cli_output_shape():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch(
            "tools.voice_live_tools.shutil.which",
            return_value="/home/piet/.local/bin/hermes-memsearch-recall",
        ),
        patch(
            "tools.voice_live_tools.subprocess.run",
            return_value=_proc(_REAL_MEMSEARCH_OUTPUT),
        ) as run,
    ):
        result = await executor.execute(
            "recall_memory", {"frage": "voice plan g dictate ime"}
        )

    assert run.call_args.args[0] == [
        "/home/piet/.local/bin/hermes-memsearch-recall",
        "-k",
        "5",
        "voice plan g dictate ime",
    ]
    assert result == {"memories": _REAL_MEMSEARCH_OUTPUT.strip()}


@pytest.mark.asyncio
async def test_recall_memory_truncates_at_word_boundary():
    long_output = "wort " * 400  # well over _RECALL_MEMORY_MAX_CHARS (1500)
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch(
            "tools.voice_live_tools.shutil.which",
            return_value="/usr/local/bin/hermes-memsearch-recall",
        ),
        patch(
            "tools.voice_live_tools.subprocess.run",
            return_value=_proc(long_output),
        ),
    ):
        result = await executor.execute("recall_memory", {"frage": "test"})

    memories = result["memories"]
    assert len(memories) <= 1_501  # max chars + the trailing ellipsis char
    assert memories.endswith("…")
    assert not memories[:-1].endswith(" ")


@pytest.mark.asyncio
async def test_recall_memory_missing_frage_is_invalid_arguments():
    executor = VoiceToolExecutor(delegate=None)
    result = await executor.execute("recall_memory", {"frage": " "})
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_recall_memory_unavailable_when_binary_not_found():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch("tools.voice_live_tools.shutil.which", return_value=None),
        patch(
            "tools.voice_live_tools._RECALL_MEMORY_FALLBACK_BIN",
            Path("/definitely/not/a/real/path/hermes-memsearch-recall"),
        ),
    ):
        result = await executor.execute("recall_memory", {"frage": "test"})

    assert result["error"]["code"] == "memory_unavailable"


@pytest.mark.asyncio
async def test_recall_memory_falls_back_to_local_bin_when_not_on_path(tmp_path):
    fallback = tmp_path / "hermes-memsearch-recall"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch("tools.voice_live_tools.shutil.which", return_value=None),
        patch("tools.voice_live_tools._RECALL_MEMORY_FALLBACK_BIN", fallback),
        patch(
            "tools.voice_live_tools.subprocess.run", return_value=_proc("some memory")
        ) as run,
    ):
        result = await executor.execute("recall_memory", {"frage": "test"})

    assert run.call_args.args[0][0] == str(fallback)
    assert result == {"memories": "some memory"}


@pytest.mark.asyncio
async def test_recall_memory_timeout_returns_structured_error():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch(
            "tools.voice_live_tools.shutil.which",
            return_value="/usr/local/bin/hermes-memsearch-recall",
        ),
        patch(
            "tools.voice_live_tools.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="hermes-memsearch-recall", timeout=10),
        ),
    ):
        result = await executor.execute("recall_memory", {"frage": "test"})

    assert result["error"]["code"] == "timeout"


@pytest.mark.asyncio
async def test_recall_memory_nonzero_exit_returns_structured_error():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch(
            "tools.voice_live_tools.shutil.which",
            return_value="/usr/local/bin/hermes-memsearch-recall",
        ),
        patch(
            "tools.voice_live_tools.subprocess.run",
            return_value=_proc("", rc=2, stderr="error: query required"),
        ),
    ):
        result = await executor.execute("recall_memory", {"frage": "test"})

    assert result["error"]["code"] == "recall_failed"
    assert "query required" in result["error"]["stderr"]


@pytest.mark.asyncio
async def test_recall_memory_empty_output_reports_no_memories_found():
    executor = VoiceToolExecutor(delegate=None)
    with (
        patch(
            "tools.voice_live_tools.shutil.which",
            return_value="/usr/local/bin/hermes-memsearch-recall",
        ),
        patch("tools.voice_live_tools.subprocess.run", return_value=_proc("   ")),
    ):
        result = await executor.execute("recall_memory", {"frage": "test"})

    assert result == {"memories": "Keine Erinnerungen gefunden."}
