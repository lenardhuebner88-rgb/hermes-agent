"""Fork-owned hint-aware wrapper around ``tools.transcription_tools``.

Why this module exists: upstream's ``transcribe_audio(file_path, model=None)``
has no ``language``/``initial_prompt`` parameters. The dictation upload
endpoint (``/api/audio/transcribe``, used by the Android app) accepts both as
optional quality hints and previously passed them straight to
``transcribe_audio()`` as extra kwargs — which raised ``TypeError`` for every
hinted request (live 502, 2026-08-06). Per this repo's fork rule, the fix
lives in its own module rather than growing upstream-owned
``hermes_cli/web_server.py`` or ``tools/transcription_tools.py``.

Dispatch:
  - No hints at all → delegate byte-for-byte to
    ``tools.voice_mode.transcribe_recording`` (unchanged behavior for the
    common, unhinted case — same hallucination filtering and
    empty-transcript-as-silence handling as before this module existed).
  - Hints given, provider is groq (the currently configured provider) →
    ``_transcribe_groq_with_hints`` calls the Groq Whisper API directly.
    Upstream's ``_transcribe_groq(file_path, model_name)`` takes no
    language/prompt kwargs and only ever resolves language from static
    config, so a per-request hint has no functioning hook to attach to
    there; rewriting that upstream handler would violate the fork-code
    placement rule, hence the dedicated call here.
  - Hints given, provider's private handler signature accepts ``language``
    (openai / mistral / xai / elevenlabs / local_command, verified via AST) →
    call it directly with the hint passed through as a kwarg.
  - Any other provider (local, deepinfra, user command providers, ...), or a
    hint-capable handler that isn't reachable for some other reason → fall
    back to the plain ``transcribe_audio()`` call. A provider that can't take
    a hint must still return a transcript, never raise.

Known caveat (out of scope for this fix, worth flagging): the openai/
mistral/xai/elevenlabs/local_command handlers all immediately overwrite their
own ``language`` parameter by re-resolving it from ``stt.<provider>.language``
/ ``stt.language`` config — so today, a per-request language hint only has a
real effect for groq (via the dedicated call below); for the other four it is
passed through faithfully but currently discarded by the callee. That is an
existing upstream inconsistency, not introduced here.
"""

import inspect
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from tools import transcription_tools

logger = logging.getLogger(__name__)

# Providers whose private handler signature accepts a `language` kwarg
# (verified against tools/transcription_tools.py via AST/rg — re-check after
# an upstream sync).
_HINT_CAPABLE_HANDLERS = {
    "openai": "_transcribe_openai",
    "mistral": "_transcribe_mistral",
    "xai": "_transcribe_xai",
    "elevenlabs": "_transcribe_elevenlabs",
    "local_command": "_transcribe_local_command",
}


def _resolve_hint_capable_model(
    provider: str, stt_config: Dict[str, Any], model: Optional[str]
) -> str:
    """Mirror transcribe_audio()'s per-provider model default, for providers we hint-dispatch to directly."""
    if provider == "openai":
        openai_cfg = stt_config.get("openai") or {}
        return model or openai_cfg.get("model", transcription_tools.DEFAULT_STT_MODEL)
    if provider == "mistral":
        mistral_cfg = stt_config.get("mistral") or {}
        return model or mistral_cfg.get("model", transcription_tools.DEFAULT_MISTRAL_STT_MODEL)
    if provider == "xai":
        # xAI Grok STT doesn't use a model parameter — pass through for logging.
        return model or "grok-stt"
    if provider == "elevenlabs":
        elevenlabs_cfg = stt_config.get("elevenlabs") or {}
        return model or elevenlabs_cfg.get(
            "model_id", transcription_tools.DEFAULT_ELEVENLABS_STT_MODEL
        )
    if provider == "local_command":
        local_cfg = stt_config.get("local") or {}
        return transcription_tools._normalize_local_command_model(
            model or local_cfg.get("model", transcription_tools.DEFAULT_LOCAL_MODEL)
        )
    raise ValueError(f"not a hint-capable provider: {provider}")


def _transcribe_groq_with_hints(
    file_path: str,
    model_name: str,
    *,
    language: Optional[str],
    initial_prompt: Optional[str],
) -> Dict[str, Any]:
    """Call Groq's OpenAI-compatible Whisper endpoint directly with language/prompt hints.

    ``tools.transcription_tools._transcribe_groq(file_path, model_name)`` takes
    no language/prompt kwargs at all, so per-request hints need their own call
    here instead of a rewrite of the upstream handler.
    """
    api_key = transcription_tools.get_env_value("GROQ_API_KEY")
    if not api_key:
        return {"success": False, "transcript": "", "error": "GROQ_API_KEY not set"}

    if not transcription_tools._HAS_OPENAI:
        return {"success": False, "transcript": "", "error": "openai package not installed"}

    resolved_model = model_name
    if resolved_model in transcription_tools.OPENAI_MODELS:
        logger.info(
            "Model %s not available on Groq, using %s",
            resolved_model,
            transcription_tools.DEFAULT_GROQ_STT_MODEL,
        )
        resolved_model = transcription_tools.DEFAULT_GROQ_STT_MODEL

    try:
        from openai import OpenAI, APIError, APIConnectionError, APITimeoutError

        client = OpenAI(
            api_key=api_key,
            base_url=transcription_tools.GROQ_BASE_URL,
            timeout=30,
            max_retries=0,
        )
        try:
            create_kwargs: Dict[str, Any] = {
                "model": resolved_model,
                "response_format": "text",
            }
            if language:
                create_kwargs["language"] = language
            if initial_prompt:
                create_kwargs["prompt"] = initial_prompt
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    file=audio_file,
                    **create_kwargs,
                )

            transcript_text = str(transcription).strip()
            logger.info(
                "Transcribed %s via Groq API with hints (%s, lang=%s, prompt=%s, %d chars)",
                Path(file_path).name,
                resolved_model,
                language or "auto",
                bool(initial_prompt),
                len(transcript_text),
            )

            return {"success": True, "transcript": transcript_text, "provider": "groq"}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except APIConnectionError as e:
        return {"success": False, "transcript": "", "error": f"Connection error: {e}"}
    except APITimeoutError as e:
        return {"success": False, "transcript": "", "error": f"Request timeout: {e}"}
    except APIError as e:
        return {"success": False, "transcript": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error("Groq transcription with hints failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Transcription failed: {e}"}


def transcribe_with_hints(
    file_path: str,
    *,
    language: Optional[str] = None,
    initial_prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe ``file_path``, forwarding language/initial_prompt hints where a provider can use them.

    Never raises on account of an unsupported hint — falls back to the plain,
    hint-less transcription path instead. Returns the same
    ``{"success", "transcript", ...}`` shape as ``transcribe_audio()``.
    """
    language = (language or "").strip() or None
    initial_prompt = (initial_prompt or "").strip() or None

    if not language and not initial_prompt:
        from tools.voice_mode import transcribe_recording

        return transcribe_recording(file_path, model=model)

    stt_config = transcription_tools._load_stt_config()
    provider = transcription_tools._get_provider(stt_config)

    if provider == "groq":
        groq_cfg = stt_config.get("groq") or {}
        model_name = (
            model or groq_cfg.get("model") or transcription_tools.DEFAULT_GROQ_STT_MODEL
        )
        return _transcribe_groq_with_hints(
            file_path, model_name, language=language, initial_prompt=initial_prompt
        )

    handler_name = _HINT_CAPABLE_HANDLERS.get(provider)
    if handler_name:
        handler = getattr(transcription_tools, handler_name)
        model_name = _resolve_hint_capable_model(provider, stt_config, model)
        kwargs: Dict[str, Any] = {}
        if language and "language" in inspect.signature(handler).parameters:
            kwargs["language"] = language
        return handler(file_path, model_name, **kwargs)

    # Provider can't take either hint (local, deepinfra, a user command
    # provider, "none", ...) — fall back to the plain path rather than crash.
    return transcription_tools.transcribe_audio(file_path, model=model)
