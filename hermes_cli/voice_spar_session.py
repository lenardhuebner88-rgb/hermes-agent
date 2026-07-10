"""Voice Sparmodus (Kaskade): STT (faster-whisper) -> LLM-CLI-Lane -> TTS (Piper).

Additive companion to :mod:`hermes_cli.voice_live_session` — the Gemini Live
path is untouched. This module owns the pieces that only the cascade needs:
local speech-to-text, a subprocription LLM-CLI turn (codex/claude, never
OpenRouter, never a raw API key), and a small text-based tool-call format so
the CLI-lane model — which gets no native function-calling — can still drive
:class:`tools.voice_live_tools.VoiceToolExecutor`.

Walkie-talkie UX: one full user turn (a bounded PCM16/16kHz recording) in,
one full spoken reply out — no barge-in, no partial streaming.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from tools.voice_live_tools import VoiceToolExecutor

_log = logging.getLogger(__name__)

_BILLING_ENV_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
_MAX_HISTORY_TURNS = 6
_VALID_LLM_LANES = ("codex", "claude")

SPAR_SYSTEM_INSTRUCTION = (
    "Du bist Hermes, Piets persönlicher Sprachassistent im Sparmodus (eine "
    "günstige Kaskade statt der Live-API). Sprich Deutsch, außer Piet "
    "wechselt die Sprache. Deine Antworten werden vorgelesen: antworte in "
    "einem bis drei kurzen Sätzen, keine Listen, keine Markdown-Zeichen. "
    "Du hast Werkzeuge: tmux-Terminals (einen Befehl an ein Ziel senden), "
    "Delegation größerer Aufgaben an den Hermes-Agenten, und look_closely "
    "für einen aktuellen Blick auf eine geteilte Kamera- oder "
    "Bildschirmansicht, und recall_memory für dein Langzeitgedächtnis über "
    "frühere Gespräche mit Claude Code, Codex und Hermes — nutze es, bevor "
    "du rätst, wenn Piet sich auf Früheres bezieht. Laufende Beobachtung "
    "(watch_view) ist im Sparmodus NICHT verfügbar, weil es hier keinen "
    "dauerhaften Live-Kanal gibt — sag das ehrlich, wenn danach gefragt "
    "wird. "
    "Um ein Werkzeug zu benutzen, schreibe GENAU EINE Zeile im Format "
    '\'TOOL: <name> <JSON-Argumente>\', zum Beispiel '
    '\'TOOL: send_to_terminal {"session": "work", "command": "ls"}\'. Sonst '
    "keinen Text in dieser Antwort — du bekommst danach das Ergebnis und "
    "antwortest in einem zweiten Schritt normal. Wenn kein Werkzeug nötig "
    "ist, antworte direkt in normalem Text. Wenn du dir etwas nicht sicher "
    "bist, sag es ehrlich."
)

_TOOL_CALL_PATTERN = re.compile(
    r"^[ \t]*TOOL:[ \t]*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)[ \t]*(?P<args>\{.*\})?[ \t]*$",
    re.MULTILINE,
)


class LlmLaneError(RuntimeError):
    """A subscription-lane LLM turn (codex/claude CLI) failed."""


# ---------------------------------------------------------------------------
# STT — faster-whisper, local, $0 marginal cost
# ---------------------------------------------------------------------------

_whisper_models: dict[str, Any] = {}


def _load_whisper_model(model_size: str) -> Any:
    """Lazy-load and cache one faster-whisper model per size, CPU-int8 default.

    Mirrors the CUDA-then-CPU-fallback pattern in
    ``tools.transcription_tools._load_local_whisper_model`` but keeps its own
    cache, decoupled from the global ``stt.provider`` config: the spar
    cascade must always use the locally installed whisper model configured
    under ``voice_web.spar``, regardless of what STT provider messaging/voice
    uses elsewhere.
    """
    cached = _whisper_models.get(model_size)
    if cached is not None:
        return cached
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(model_size, device="auto", compute_type="auto")
    except Exception:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    _whisper_models[model_size] = model
    return model


def transcribe_wav(wav_path: str, *, model_size: str, language: str | None = None) -> str:
    """Transcribe a PCM16 WAV file with faster-whisper. Blocking — call via a thread."""
    model = _load_whisper_model(model_size)
    kwargs: dict[str, Any] = {"beam_size": 5}
    if language:
        # voice_web.language is a locale ("de-DE"); faster-whisper wants "de".
        kwargs["language"] = language.split("-")[0]
    segments, _info = model.transcribe(wav_path, **kwargs)
    return " ".join(segment.text.strip() for segment in segments).strip()


# ---------------------------------------------------------------------------
# TTS — Piper, local, $0 marginal cost
# ---------------------------------------------------------------------------


def synthesize_to_wav(text: str, *, voice_path: str, output_path: Path) -> None:
    """Synthesize *text* to a WAV file at *output_path* via Piper. Blocking."""
    from tools.tts_tool import _generate_piper_tts

    _generate_piper_tts(text, str(output_path), {"piper": {"voice": voice_path}})


# ---------------------------------------------------------------------------
# LLM lane — codex/claude CLI, subscription billing only
# ---------------------------------------------------------------------------


def resolve_claude_bin() -> str:
    return os.environ.get("HERMES_CLAUDE_BIN") or shutil.which("claude") or "claude"


def resolve_codex_bin() -> str:
    return os.environ.get("HERMES_CODEX_BIN") or shutil.which("codex") or "codex"


def _subscription_env() -> dict[str, str]:
    """A copy of the process env with any provider API key stripped.

    Both CLI lanes run on their subscription/OAuth login; a stray
    ``ANTHROPIC_API_KEY``/``OPENAI_API_KEY`` in the environment would
    silently switch billing to metered API usage, defeating the entire
    point of the $0-marginal-cost Sparmodus.
    """
    env = dict(os.environ)
    for key in _BILLING_ENV_KEYS:
        env.pop(key, None)
    return env


def build_claude_command(prompt: str, *, model: str | None, claude_bin: str | None = None) -> list[str]:
    cmd = [
        claude_bin or resolve_claude_bin(),
        "-p",
        prompt,
        "--output-format",
        "json",
        "--disallowedTools",
        "Bash,Read,Write,Edit,MultiEdit,NotebookEdit,Glob,Grep,WebFetch,WebSearch,Task",
        "--strict-mcp-config",
        "--settings",
        '{"enabledPlugins": {"memsearch@memsearch-plugins": false}}',
    ]
    if model:
        cmd.extend(["--model", model])
    return cmd


def build_claude_stream_command(
    *, model: str | None, system_instruction: str, claude_bin: str | None = None
) -> list[str]:
    """The persistent claude-lane child: one long-lived process per Spar-Session.

    ``--input-format/--output-format stream-json`` keeps stdin/stdout open
    across turns instead of exiting after one reply, so only the *first*
    turn pays the ~5s CLI-startup cost (see ``PersistentClaudeLane``). The
    system prompt is set once here rather than resent per turn.
    """
    cmd = [
        claude_bin or resolve_claude_bin(),
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--system-prompt",
        system_instruction,
        "--disallowedTools",
        "Bash,Read,Write,Edit,MultiEdit,NotebookEdit,Glob,Grep,WebFetch,WebSearch,Task",
        "--strict-mcp-config",
        "--settings",
        '{"enabledPlugins": {"memsearch@memsearch-plugins": false}}',
    ]
    if model:
        cmd.extend(["--model", model])
    return cmd


def build_codex_command(
    prompt: str, *, model: str | None, output_file: str, codex_bin: str | None = None
) -> list[str]:
    cmd = [codex_bin or resolve_codex_bin(), "exec"]
    if model:
        cmd.extend(["-m", model])
    cmd.extend(
        [
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-o",
            output_file,
            prompt,
        ]
    )
    return cmd


async def _run_subprocess(
    cmd: list[str], *, env: dict[str, str], cwd: str, timeout: float
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise LlmLaneError(f"LLM-Lane konnte nicht gestartet werden: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise LlmLaneError("LLM-Lane hat das Zeitlimit überschritten.") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[-500:]
        raise LlmLaneError(f"LLM-Lane-Fehler (exit {process.returncode}): {detail}")
    return stdout.decode("utf-8", "replace")


def _extract_claude_result(stdout: str) -> str:
    raw = stdout.strip()
    if not raw:
        raise LlmLaneError("Claude-Lane lieferte keine Antwort.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
    raise LlmLaneError("Claude-Lane lieferte keine gültige JSON-Antwort.")


async def call_llm_lane(
    lane: str,
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = 25.0,
    cwd: str | None = None,
) -> str:
    """Run one stateless LLM turn on the codex or claude subscription CLI lane.

    Stateless-with-rolling-transcript by design (see ``build_prompt``): each
    call spawns a fresh CLI process and pays its ~5s startup cost. This is
    what the codex lane still uses for every turn (``StatelessLlmLane``) and
    what the claude lane falls back to before a ``PersistentClaudeLane`` has
    started — the persistent lane pays this cost only once per session.
    """
    if lane not in _VALID_LLM_LANES:
        raise LlmLaneError(f"Unbekannte llm_lane: {lane!r}")
    env = _subscription_env()
    work_dir = cwd or str(Path.home())
    if lane == "claude":
        stdout = await _run_subprocess(
            build_claude_command(prompt, model=model), env=env, cwd=work_dir, timeout=timeout
        )
        return _extract_claude_result(stdout)

    fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="hermes-voice-spar-")
    os.close(fd)
    try:
        await _run_subprocess(
            build_codex_command(prompt, model=model, output_file=output_path),
            env=env,
            cwd=work_dir,
            timeout=timeout,
        )
        text = Path(output_path).read_text(encoding="utf-8", errors="replace").strip()
    finally:
        Path(output_path).unlink(missing_ok=True)
    if not text:
        raise LlmLaneError("Codex-Lane lieferte keine Antwort.")
    return text


# ---------------------------------------------------------------------------
# Tool-call format + prompt building + turn loop
# ---------------------------------------------------------------------------

HistoryTurn = tuple[str, str]


def parse_tool_call(text: str) -> tuple[str | None, dict[str, Any]]:
    """Parse the first ``TOOL: <name> <json-args>`` line, if any."""
    match = _TOOL_CALL_PATTERN.search(text)
    if match is None:
        return None, {}
    name = match.group("name")
    raw_args = match.group("args")
    if not raw_args:
        return name, {}
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return name, {}
    return name, (parsed if isinstance(parsed, dict) else {})


def _strip_tool_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not _TOOL_CALL_PATTERN.match(line)
    ).strip()


def build_prompt(
    system_instruction: str, history: list[HistoryTurn], user_text: str
) -> str:
    lines = [system_instruction, ""]
    trimmed = history[-_MAX_HISTORY_TURNS:]
    if trimmed:
        lines.append("Bisheriger Gesprächsverlauf:")
        for role, text in trimmed:
            speaker = "Piet" if role == "user" else "Assistent"
            lines.append(f"{speaker}: {text}")
        lines.append("")
    lines.append(f"Piet: {user_text}")
    lines.append("Assistent:")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM lane — one interface, two lifecycles
# ---------------------------------------------------------------------------
#
# Both the codex and the claude lane sit behind the same ``SparLlmLane``
# interface so ``run_turn`` never has to know which one it's driving. They
# differ in *lifecycle*, not in what they're asked to do:
#
# - ``StatelessLlmLane`` (codex, and claude before a persistent child is
#   available): one fresh CLI subprocess per turn, full rolling transcript
#   resent as the prompt every time (``build_prompt``) since nothing survives
#   between calls.
# - ``PersistentClaudeLane``: one ``claude`` child kept alive for the whole
#   voice session (``--input-format/--output-format stream-json``). The
#   system prompt is set once at spawn and conversation context lives in the
#   child, so normal turns send only the new user text — no resent
#   transcript, no per-turn CLI startup cost. The *server* still keeps
#   ``history`` (passed into ``turn()``) purely as a crash-recovery net: if
#   the child dies mid-session, one restart is attempted and the child is
#   caught back up with a single ``build_prompt`` message built from that
#   server-side history, then normal incremental turns resume.

_TOOL_RESULT_CONTINUATION = (
    "[Werkzeug-Ergebnis von {name}]: {result}\n\n"
    "Antworte jetzt in normalem Text (keine weitere TOOL-Zeile, außer "
    "ein weiteres Werkzeug ist wirklich nötig)."
)


class SparLlmLane(abc.ABC):
    """One active LLM lane for a Sparmodus session (stateless or persistent)."""

    @abc.abstractmethod
    async def turn(self, user_text: str, *, history: list[HistoryTurn]) -> str:
        """Start a new logical turn and return the raw (unparsed) reply."""

    @abc.abstractmethod
    async def continue_with_tool_result(
        self, tool_name: str, tool_result: dict[str, Any]
    ) -> str:
        """Continue the in-flight turn after a tool hop; return the raw reply."""

    async def start(self) -> None:
        """Warm up the lane at session-begin (no-op for stateless lanes)."""
        return None

    async def aclose(self) -> None:
        """Release any lane resources (no-op for stateless lanes)."""
        return None


class StatelessLlmLane(SparLlmLane):
    """One fresh CLI subprocess per turn — the pre-persistent-child behaviour."""

    def __init__(
        self,
        lane: str,
        *,
        model: str | None,
        timeout: float,
        cwd: str | None,
        system_instruction: str,
        claude_bin: str | None = None,
        codex_bin: str | None = None,
    ) -> None:
        self._lane = lane
        self._model = model
        self._timeout = timeout
        self._cwd = cwd
        self._system_instruction = system_instruction
        self._claude_bin = claude_bin
        self._codex_bin = codex_bin
        self._current_prompt: str | None = None
        self._last_reply: str = ""

    async def turn(self, user_text: str, *, history: list[HistoryTurn]) -> str:
        self._current_prompt = build_prompt(self._system_instruction, history, user_text)
        return await self._call()

    async def continue_with_tool_result(
        self, tool_name: str, tool_result: dict[str, Any]
    ) -> str:
        if self._current_prompt is None:
            raise LlmLaneError("continue_with_tool_result vor turn() aufgerufen.")
        self._current_prompt = (
            f"{self._current_prompt} {self._last_reply}\n\n"
            + _TOOL_RESULT_CONTINUATION.format(
                name=tool_name,
                result=json.dumps(tool_result, ensure_ascii=False),
            )
        )
        return await self._call()

    async def _call(self) -> str:
        assert self._current_prompt is not None
        reply = await call_llm_lane(
            self._lane,
            self._current_prompt,
            model=self._model,
            timeout=self._timeout,
            cwd=self._cwd,
        )
        self._last_reply = reply
        return reply


class PersistentClaudeLane(SparLlmLane):
    """One long-lived ``claude`` child (stream-json) for the whole session.

    Lifecycle: ``start()`` spawns the child (call this at session-begin so
    the ~5s CLI startup overlaps with the first STT wait, not the first
    reply). Every ``turn``/``continue_with_tool_result`` call sends one
    stream-json line on stdin and reads lines from stdout until a
    ``{"type": "result", ...}`` event. On a dead/broken child, exactly one
    restart is attempted (fresh spawn + a catch-up message built from the
    server-side ``history``); a second failure raises ``LlmLaneError``.
    ``aclose()`` is best-effort terminate→kill and safe to call more than
    once (no zombie: always awaits the child's exit).
    """

    def __init__(
        self,
        *,
        model: str | None,
        timeout: float,
        cwd: str | None,
        system_instruction: str,
        claude_bin: str | None = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._cwd = cwd or str(Path.home())
        self._system_instruction = system_instruction
        self._claude_bin = claude_bin
        self._process: asyncio.subprocess.Process | None = None
        self._restarted = False
        self._last_history: list[HistoryTurn] = []

    async def start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        self._process = await self._spawn()

    async def _spawn(self) -> asyncio.subprocess.Process:
        cmd = build_claude_stream_command(
            model=self._model,
            system_instruction=self._system_instruction,
            claude_bin=self._claude_bin,
        )
        env = _subscription_env()
        try:
            return await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise LlmLaneError(f"Claude-Lane konnte nicht gestartet werden: {exc}") from exc

    async def turn(self, user_text: str, *, history: list[HistoryTurn]) -> str:
        self._last_history = history
        return await self._send_with_restart(user_text, history=history)

    async def continue_with_tool_result(
        self, tool_name: str, tool_result: dict[str, Any]
    ) -> str:
        text = _TOOL_RESULT_CONTINUATION.format(
            name=tool_name,
            result=json.dumps(tool_result, ensure_ascii=False),
        )
        return await self._send_with_restart(text, history=self._last_history)

    async def _send_with_restart(self, text: str, *, history: list[HistoryTurn]) -> str:
        if self._process is None:
            await self.start()
        try:
            return await self._send_line(text)
        except LlmLaneError:
            if self._restarted:
                raise
            self._restarted = True
            _log.warning("Claude-Lane-Kind abgestürzt; ein Neustartversuch.")
            await self._terminate()
            self._process = await self._spawn()
            # The fresh child has no memory of the session; catch it up with
            # one message built from everything the server tracked so far.
            return await self._send_line(
                build_prompt(self._system_instruction, history, text)
            )

    async def _send_line(self, text: str) -> str:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise LlmLaneError("Claude-Lane-Prozess ist nicht aktiv.")
        if process.returncode is not None:
            raise LlmLaneError("Claude-Lane-Prozess ist bereits beendet.")
        payload = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )
        try:
            process.stdin.write((payload + "\n").encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise LlmLaneError(f"Claude-Lane-Prozess ist nicht erreichbar: {exc}") from exc
        return await self._read_result(process)

    async def _read_result(self, process: asyncio.subprocess.Process) -> str:
        assert process.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise LlmLaneError("Claude-Lane hat das Zeitlimit überschritten.")
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            except TimeoutError as exc:
                raise LlmLaneError("Claude-Lane hat das Zeitlimit überschritten.") from exc
            if not line:
                raise LlmLaneError("Claude-Lane-Prozess wurde unerwartet beendet.")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            if event.get("is_error"):
                detail = event.get("result") or event.get("subtype") or "unbekannter Fehler"
                raise LlmLaneError(f"Claude-Lane-Fehler: {detail}")
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip()
            raise LlmLaneError("Claude-Lane lieferte keine gültige Antwort.")

    async def _terminate(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (RuntimeError, OSError):
            pass
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
            return
        except TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            _log.warning("Claude-Lane-Kind reagiert nicht auf SIGKILL.")

    async def aclose(self) -> None:
        await self._terminate()


def create_llm_lane(
    llm_lane: str,
    *,
    model: str | None,
    timeout: float,
    cwd: str | None = None,
    system_instruction: str,
    claude_bin: str | None = None,
    codex_bin: str | None = None,
) -> SparLlmLane:
    """Build the ``SparLlmLane`` for one voice session (one call per session)."""
    if llm_lane not in _VALID_LLM_LANES:
        raise LlmLaneError(f"Unbekannte llm_lane: {llm_lane!r}")
    if llm_lane == "claude":
        return PersistentClaudeLane(
            model=model,
            timeout=timeout,
            cwd=cwd,
            system_instruction=system_instruction,
            claude_bin=claude_bin,
        )
    return StatelessLlmLane(
        llm_lane,
        model=model,
        timeout=timeout,
        cwd=cwd,
        system_instruction=system_instruction,
        codex_bin=codex_bin,
    )


async def run_turn(
    user_text: str,
    *,
    history: list[HistoryTurn],
    lane: SparLlmLane,
    executor: VoiceToolExecutor,
    max_tool_hops: int = 2,
) -> tuple[str, list[HistoryTurn]]:
    """Run one full turn: LLM call, then up to ``max_tool_hops`` tool round-trips."""
    raw_reply = await lane.turn(user_text, history=history)
    hops = 0
    while True:
        tool_name, tool_args = parse_tool_call(raw_reply)
        if tool_name is None or hops >= max_tool_hops:
            final_text = _strip_tool_lines(raw_reply) or raw_reply.strip()
            new_history = [*history, ("user", user_text), ("assistant", final_text)]
            return final_text, new_history
        hops += 1
        tool_result = await executor.execute(tool_name, tool_args)
        raw_reply = await lane.continue_with_tool_result(tool_name, tool_result)
