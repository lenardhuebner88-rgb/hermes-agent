import wave
from pathlib import Path

import pytest

from hermes_cli.voice_spar_session import (
    SPAR_SYSTEM_INSTRUCTION,
    LlmLaneError,
    PersistentClaudeLane,
    StatelessLlmLane,
    _extract_claude_result,
    _subscription_env,
    build_claude_command,
    build_claude_stream_command,
    build_codex_command,
    build_prompt,
    call_llm_lane,
    create_llm_lane,
    parse_tool_call,
    run_turn,
    synthesize_to_wav,
    transcribe_wav,
)

FIXTURE_PCM = Path(__file__).parent / "fixtures" / "voice_sample_16k.pcm"
FAKE_CLAUDE_STREAM_CLI = Path(__file__).parent / "fixtures" / "fake_claude_stream_cli.py"


# ---------------------------------------------------------------------------
# Tool-call format
# ---------------------------------------------------------------------------


def test_parse_tool_call_extracts_name_and_json_args():
    name, args = parse_tool_call(
        'TOOL: send_to_terminal {"session": "work", "command": "ls"}'
    )
    assert name == "send_to_terminal"
    assert args == {"session": "work", "command": "ls"}


def test_parse_tool_call_no_match_returns_none():
    name, args = parse_tool_call("Ich habe keine Werkzeuge nötig.")
    assert name is None
    assert args == {}


def test_parse_tool_call_no_args_returns_empty_dict():
    name, args = parse_tool_call("TOOL: hermes_status")
    assert name == "hermes_status"
    assert args == {}


def test_parse_tool_call_bad_json_falls_back_to_bare_name():
    name, args = parse_tool_call("TOOL: send_to_terminal {not json}")
    assert name == "send_to_terminal"
    assert args == {}


def test_parse_tool_call_finds_line_within_surrounding_text():
    text = 'Klar, einen Moment.\nTOOL: hermes_status {}\n'
    name, args = parse_tool_call(text)
    assert name == "hermes_status"
    assert args == {}


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def test_build_prompt_includes_system_instruction_and_user_text():
    prompt = build_prompt(SPAR_SYSTEM_INSTRUCTION, [], "Wie spät ist es?")
    assert prompt.startswith(SPAR_SYSTEM_INSTRUCTION)
    assert "Piet: Wie spät ist es?" in prompt
    assert prompt.endswith("Assistent:")


def test_build_prompt_includes_recent_history():
    history = [("user", "Hallo"), ("assistant", "Hallo Piet")]
    prompt = build_prompt("System.", history, "Und jetzt?")
    assert "Piet: Hallo" in prompt
    assert "Assistent: Hallo Piet" in prompt
    assert "Piet: Und jetzt?" in prompt


def test_build_prompt_trims_history_to_last_turns():
    history = [("user", f"turn-{i}") for i in range(20)]
    prompt = build_prompt("System.", history, "neu")
    assert "turn-0" not in prompt
    assert "turn-19" in prompt


# ---------------------------------------------------------------------------
# LLM lane command construction (never OpenRouter, never a raw API key)
# ---------------------------------------------------------------------------


def test_build_claude_command_shape():
    cmd = build_claude_command("hallo", model=None, claude_bin="/bin/claude")
    assert cmd[0] == "/bin/claude"
    assert "-p" in cmd
    assert "hallo" in cmd
    assert "--output-format" in cmd
    idx = cmd.index("--output-format")
    assert cmd[idx + 1] == "json"
    assert "--model" not in cmd


def test_build_claude_command_includes_model_override():
    cmd = build_claude_command("hallo", model="claude-haiku-5", claude_bin="/bin/claude")
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5"


def test_build_codex_command_shape():
    cmd = build_codex_command(
        "hallo", model=None, output_file="/tmp/out.txt", codex_bin="/bin/codex"
    )
    assert cmd[0] == "/bin/codex"
    assert cmd[1] == "exec"
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "-o" in cmd
    assert cmd[cmd.index("-o") + 1] == "/tmp/out.txt"
    assert cmd[-1] == "hallo"
    assert "-m" not in cmd


def test_build_codex_command_includes_model_override():
    cmd = build_codex_command(
        "hallo", model="gpt-5.4-mini", output_file="/tmp/out.txt", codex_bin="/bin/codex"
    )
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.4-mini"


def test_subscription_env_strips_billing_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = _subscription_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert env.get("SOME_OTHER_VAR") == "keep-me"


def test_extract_claude_result_parses_result_field():
    assert _extract_claude_result('{"type":"result","result":"Hallo Piet"}') == "Hallo Piet"


def test_extract_claude_result_falls_back_to_raw_text_on_non_json():
    assert _extract_claude_result("Hallo Piet") == "Hallo Piet"


def test_extract_claude_result_raises_on_empty_output():
    with pytest.raises(LlmLaneError):
        _extract_claude_result("   ")


def test_extract_claude_result_raises_on_json_without_result():
    with pytest.raises(LlmLaneError):
        _extract_claude_result('{"type":"result"}')


@pytest.mark.asyncio
async def test_call_llm_lane_rejects_unknown_lane():
    with pytest.raises(LlmLaneError):
        await call_llm_lane("openrouter", "hallo")


@pytest.mark.asyncio
async def test_call_llm_lane_claude_invokes_subprocess_and_parses_json(monkeypatch):
    import hermes_cli.voice_spar_session as spar

    captured = {}

    async def fake_run_subprocess(cmd, *, env, cwd, timeout):
        captured["cmd"] = cmd
        captured["env"] = env
        assert "ANTHROPIC_API_KEY" not in env
        return '{"type":"result","result":"Hallo Piet"}'

    monkeypatch.setattr(spar, "_run_subprocess", fake_run_subprocess)
    reply = await call_llm_lane("claude", "hallo", timeout=5.0)
    assert reply == "Hallo Piet"
    assert "-p" in captured["cmd"]


@pytest.mark.asyncio
async def test_call_llm_lane_codex_reads_output_file(monkeypatch, tmp_path):
    import hermes_cli.voice_spar_session as spar

    async def fake_run_subprocess(cmd, *, env, cwd, timeout):
        output_file = cmd[cmd.index("-o") + 1]
        Path(output_file).write_text("Hallo aus Codex.\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(spar, "_run_subprocess", fake_run_subprocess)
    reply = await call_llm_lane("codex", "hallo", timeout=5.0, cwd=str(tmp_path))
    assert reply == "Hallo aus Codex."


@pytest.mark.asyncio
async def test_call_llm_lane_codex_output_file_is_cleaned_up(monkeypatch, tmp_path):
    import hermes_cli.voice_spar_session as spar

    written_paths = []

    async def fake_run_subprocess(cmd, *, env, cwd, timeout):
        output_file = cmd[cmd.index("-o") + 1]
        written_paths.append(output_file)
        Path(output_file).write_text("ok", encoding="utf-8")
        return ""

    monkeypatch.setattr(spar, "_run_subprocess", fake_run_subprocess)
    await call_llm_lane("codex", "hallo", timeout=5.0, cwd=str(tmp_path))
    assert written_paths
    assert not Path(written_paths[0]).exists()


# ---------------------------------------------------------------------------
# Tool-loop turn runner
# ---------------------------------------------------------------------------


class _FakeExecutor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def execute(self, name, args):
        self.calls.append((name, args))
        return self.result


def _codex_lane(system_instruction=SPAR_SYSTEM_INSTRUCTION):
    return create_llm_lane(
        "codex", model=None, timeout=5.0, system_instruction=system_instruction
    )


@pytest.mark.asyncio
async def test_run_turn_returns_direct_reply_without_tool_call(monkeypatch):
    import hermes_cli.voice_spar_session as spar

    async def fake_call_llm_lane(lane, prompt, *, model, timeout, cwd):
        return "Alles klar."

    monkeypatch.setattr(spar, "call_llm_lane", fake_call_llm_lane)
    executor = _FakeExecutor({"ok": True})
    reply, history = await run_turn(
        "Hallo",
        history=[],
        lane=_codex_lane(),
        executor=executor,
    )
    assert reply == "Alles klar."
    assert history == [("user", "Hallo"), ("assistant", "Alles klar.")]
    assert executor.calls == []


@pytest.mark.asyncio
async def test_run_turn_executes_tool_call_then_returns_final_text(monkeypatch):
    import hermes_cli.voice_spar_session as spar

    replies = iter(
        ['TOOL: send_to_terminal {"session": "work", "command": "ls"}', "Fertig."]
    )

    async def fake_call_llm_lane(lane, prompt, *, model, timeout, cwd):
        return next(replies)

    monkeypatch.setattr(spar, "call_llm_lane", fake_call_llm_lane)
    executor = _FakeExecutor({"ok": True})
    reply, history = await run_turn(
        "Führe ls aus",
        history=[],
        lane=_codex_lane(),
        executor=executor,
    )
    assert reply == "Fertig."
    assert executor.calls == [("send_to_terminal", {"session": "work", "command": "ls"})]
    assert history[-1] == ("assistant", "Fertig.")


@pytest.mark.asyncio
async def test_run_turn_stops_at_max_tool_hops(monkeypatch):
    import hermes_cli.voice_spar_session as spar

    async def always_tool_call(lane, prompt, *, model, timeout, cwd):
        return 'TOOL: hermes_status {}'

    monkeypatch.setattr(spar, "call_llm_lane", always_tool_call)
    executor = _FakeExecutor({"ok": True})
    reply, _history = await run_turn(
        "Status?",
        history=[],
        lane=_codex_lane(),
        executor=executor,
        max_tool_hops=2,
    )
    # After exhausting the hop budget the loop stops rather than running
    # forever; with nothing but a TOOL: line left to strip, the raw text is
    # returned as-is (never silently empty).
    assert reply == "TOOL: hermes_status {}"
    assert len(executor.calls) == 2


# ---------------------------------------------------------------------------
# SparLlmLane — one interface, two lifecycles (persistent claude, stateless
# codex). The persistent lane is exercised against a real subprocess (a
# fake "claude" CLI fixture speaking the empirically verified stream-json
# line format), never mocked at the asyncio.subprocess boundary — that
# would just re-assert our own protocol understanding back at us.
# ---------------------------------------------------------------------------


def test_build_claude_stream_command_shape():
    cmd = build_claude_stream_command(
        model=None, system_instruction="Sys.", claude_bin="/bin/claude"
    )
    assert cmd[0] == "/bin/claude"
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--system-prompt") + 1] == "Sys."
    assert "--verbose" in cmd
    assert "--model" not in cmd


def test_build_claude_stream_command_includes_model_override():
    cmd = build_claude_stream_command(
        model="claude-haiku-5", system_instruction="Sys.", claude_bin="/bin/claude"
    )
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5"


def test_create_llm_lane_claude_returns_persistent_lane():
    lane = create_llm_lane(
        "claude", model=None, timeout=5.0, system_instruction=SPAR_SYSTEM_INSTRUCTION
    )
    assert isinstance(lane, PersistentClaudeLane)


def test_create_llm_lane_codex_returns_stateless_lane():
    lane = create_llm_lane(
        "codex", model=None, timeout=5.0, system_instruction=SPAR_SYSTEM_INSTRUCTION
    )
    assert isinstance(lane, StatelessLlmLane)


def test_create_llm_lane_rejects_unknown_lane():
    with pytest.raises(LlmLaneError):
        create_llm_lane("openrouter", model=None, timeout=5.0, system_instruction="Sys.")


def _fake_persistent_lane(**overrides):
    kwargs = dict(
        model=None,
        timeout=5.0,
        cwd=None,
        system_instruction="Sys.",
        claude_bin=str(FAKE_CLAUDE_STREAM_CLI),
    )
    kwargs.update(overrides)
    return PersistentClaudeLane(**kwargs)


@pytest.mark.asyncio
async def test_persistent_claude_lane_turn_roundtrip():
    lane = _fake_persistent_lane()
    try:
        await lane.start()
        reply = await lane.turn("hallo", history=[])
        assert reply == "HALLO"
    finally:
        await lane.aclose()


@pytest.mark.asyncio
async def test_persistent_claude_lane_reuses_child_across_turns():
    lane = _fake_persistent_lane()
    try:
        await lane.start()
        process = lane._process
        reply1 = await lane.turn("eins", history=[])
        reply2 = await lane.turn(
            "zwei", history=[("user", "eins"), ("assistant", "EINS")]
        )
        assert (reply1, reply2) == ("EINS", "ZWEI")
        assert lane._process is process  # same child the whole session, no restart
    finally:
        await lane.aclose()


@pytest.mark.asyncio
async def test_persistent_claude_lane_continue_with_tool_result():
    lane = _fake_persistent_lane()
    try:
        await lane.start()
        await lane.turn("nutze ein werkzeug", history=[])
        reply = await lane.continue_with_tool_result("send_to_terminal", {"ok": True})
        assert "SEND_TO_TERMINAL" in reply
    finally:
        await lane.aclose()


@pytest.mark.asyncio
async def test_persistent_claude_lane_restarts_once_after_crash():
    lane = _fake_persistent_lane()
    try:
        await lane.start()
        dead_process = lane._process
        reply = await lane.turn(
            "CRASH-mich", history=[("user", "vorher"), ("assistant", "OK")]
        )
        assert dead_process.returncode is not None  # crashed child reaped, no zombie
        assert lane._process is not dead_process  # fresh child spawned by the restart
        assert reply  # the fresh child answered the catch-up (build_prompt) message
    finally:
        await lane.aclose()


@pytest.mark.asyncio
async def test_persistent_claude_lane_raises_after_second_crash():
    lane = _fake_persistent_lane()
    try:
        await lane.start()
        first_reply = await lane.turn("CRASH-A", history=[])
        assert first_reply  # first crash consumed the one restart attempt
        with pytest.raises(LlmLaneError):
            await lane.turn("CRASH-B", history=[])
    finally:
        await lane.aclose()


@pytest.mark.asyncio
async def test_persistent_claude_lane_aclose_terminates_idle_child_no_zombie():
    lane = _fake_persistent_lane()
    await lane.start()
    process = lane._process
    assert process.returncode is None
    await lane.aclose()
    assert process.returncode is not None
    assert lane._process is None
    # Safe to call again (session close paths call aclose() unconditionally).
    await lane.aclose()


@pytest.mark.asyncio
async def test_persistent_claude_lane_timeout_recovers_once_then_raises():
    lane = _fake_persistent_lane(timeout=0.3)
    try:
        await lane.start()
        # The first HANG times out; the lane's one restart attempt resends
        # a build_prompt-wrapped catch-up message that no longer starts
        # with "HANG-1", so the fresh child answers normally.
        reply = await lane.turn("HANG-1", history=[])
        assert reply
        # The restart budget is spent; a second stuck child raises outright.
        with pytest.raises(LlmLaneError):
            await lane.turn("HANG-2", history=[])
    finally:
        await lane.aclose()


# ---------------------------------------------------------------------------
# STT / TTS — real engines, real WAV/PCM data (skipped if not installed)
# ---------------------------------------------------------------------------


def test_transcribe_wav_real_faster_whisper_on_fixture(tmp_path):
    pytest.importorskip("faster_whisper")
    assert FIXTURE_PCM.exists()
    pcm = FIXTURE_PCM.read_bytes()
    assert len(pcm) > 20_000

    wav_path = tmp_path / "input.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(pcm)

    transcript = transcribe_wav(str(wav_path), model_size="tiny", language="de-DE")
    assert isinstance(transcript, str)
    assert transcript.strip()


def test_synthesize_to_wav_real_piper_smoke(tmp_path):
    pytest.importorskip("piper")
    from hermes_cli.voice_ws import _default_spar_piper_voice_path

    voice_path = _default_spar_piper_voice_path()
    if not Path(voice_path).is_file():
        pytest.skip(f"Piper voice not downloaded at {voice_path}")

    output_path = tmp_path / "spar-smoke.wav"
    synthesize_to_wav(
        "Guten Tag, das ist ein Testsatz für den Sparmodus.",
        voice_path=voice_path,
        output_path=output_path,
    )
    assert output_path.is_file()
    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() > 0
        assert wav_file.getnframes() > 0
