"""Runtime patches for upstream openai SDK quirks that affect Hermes.

Imported once at process start (see :mod:`agent.codex_runtime`), this module
monkey-patches the installed ``openai`` package in place.  Keeping the
adjustments here (instead of editing files under ``venv/``) means a
``uv sync`` / ``pip install openai`` does not silently undo them.

Each patch is **idempotent** — re-importing the module is a no-op — and
marks the target module with a sentinel attribute so test code and
diagnostics can confirm the patch is active.

Current patches
---------------

``parse_response`` ``None`` output guard
    The ChatGPT Codex backend (``chatgpt.com/backend-api/codex``) streams
    ``gpt-5.x`` responses where the streaming accumulator's intermediate
    ``Response`` snapshot can carry ``output=None``.  Upstream
    ``openai/lib/_parsing/_responses.py`` (verified on 2.24.0 and 2.38.0)
    assumes ``response.output`` is always a ``List`` and crashes the
    stream with ``TypeError: 'NoneType' object is not iterable`` — which
    surfaces to the user as the generic Hermes
    "Codex Responses stream returned non-iterable None" error and tears
    down the gateway turn.  The wrapper coerces ``None`` to ``[]`` before
    the upstream loop runs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _apply_openai_parse_response_none_guard() -> bool:
    """Wrap ``openai.lib._parsing._responses.parse_response`` to tolerate ``output=None``.

    Returns ``True`` if the patch was applied (or was already active),
    ``False`` if the target module could not be imported.
    """
    try:
        from openai.lib._parsing import _responses as _mod
    except ImportError:
        return False

    if getattr(_mod, "_hermes_parse_response_none_guard_applied", False):
        return True

    _orig = _mod.parse_response

    def _safe_parse_response(**kwargs):
        response = kwargs.get("response")
        if response is not None and getattr(response, "output", None) is None:
            try:
                response.output = []
            except Exception:
                pass
        return _orig(**kwargs)

    _mod.parse_response = _safe_parse_response
    _mod._hermes_parse_response_none_guard_applied = True
    logger.debug("openai_sdk_patches: parse_response None-output guard applied")
    return True


def apply_all() -> None:
    """Apply every patch in this module.  Safe to call multiple times."""
    _apply_openai_parse_response_none_guard()


apply_all()
