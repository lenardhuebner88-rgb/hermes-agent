"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, cwd) and
appends it to the FINAL message of an agent turn when enabled.  Off by default
to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true                       # off by default
        fields: [model, context_pct, cwd]   # order shown; drop any to hide

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.

The footer is appended to the final response text in ``gateway/run.py`` right
before returning the response to the adapter send path — so it only lands on
the final message a user sees, not on tool-progress updates or streaming
partials.  When streaming is on and the final text has already been delivered
piecemeal, the footer is sent as a separate trailing message via
``send_trailing_footer()``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix for readability (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def _format_token_count(tokens: int) -> str:
    """Compact token count for the runtime footer (``14800`` → ``14.8k``)."""
    if tokens < 1_000:
        return str(tokens)
    if tokens < 1_000_000:
        value = tokens / 1_000
        if value.is_integer() or value >= 100:
            return f"{value:.0f}k"
        return f"{value:.1f}k"
    value = tokens / 1_000_000
    if value.is_integer() or value >= 100:
        return f"{value:.0f}m"
    return f"{value:.1f}m"


def format_context_usage_footer(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    context_length: int | None,
    estimated: bool = False,
) -> str | None:
    """Render exact/estimated per-response token detail, or ``None``.

    ``input_tokens`` and ``output_tokens`` are expected to come from the latest
    already-normalized ``CanonicalUsage`` object. Callers should pass ``None``
    when provider usage is unavailable; this helper never invents exact token
    numbers.
    """
    if input_tokens is None or output_tokens is None:
        return None
    if context_length is None or context_length <= 0:
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None

    prefix = "~" if estimated else ""
    pct = max(0, min(100, round((input_tokens / context_length) * 100)))
    return (
        f"Kontext: {prefix}{pct} % · "
        f"{prefix}{_format_token_count(input_tokens)}/{_format_token_count(context_length)} Token · "
        f"Antwort: {output_tokens}"
    )


def resolve_token_detail_usage(agent_result: dict[str, Any]) -> tuple[int | None, int | None, bool]:
    """Return token-detail usage from exact per-turn values or aggregate fallback.

    ``last_*`` values come from the provider's normalized per-turn usage. Some
    gateway/provider paths only surface session aggregate counters; those are
    less precise for a single response, so mark them as estimated when used.
    """
    input_tokens = agent_result.get("last_input_tokens")
    output_tokens = agent_result.get("last_output_tokens")
    estimated = False

    if input_tokens is None and agent_result.get("input_tokens") is not None:
        input_tokens = agent_result.get("input_tokens")
        estimated = True
    if output_tokens is None and agent_result.get("output_tokens") is not None:
        output_tokens = agent_result.get("output_tokens")
        estimated = True

    return input_tokens, output_tokens, estimated


def resolve_footer_config(
    user_config: dict[str, Any] | None,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """Resolve effective runtime-footer config for *platform_key*.

    Merge order (later wins):
        1. Built-in defaults (enabled=False)
        2. ``display.runtime_footer``
        3. ``display.platforms.<platform_key>.runtime_footer``
    """
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}

    global_cfg = cfg.get("runtime_footer")
    if isinstance(global_cfg, dict):
        if "enabled" in global_cfg:
            resolved["enabled"] = bool(global_cfg.get("enabled"))
        if isinstance(global_cfg.get("fields"), list) and global_cfg["fields"]:
            resolved["fields"] = [str(f) for f in global_cfg["fields"]]

    if platform_key:
        platforms = cfg.get("platforms") or {}
        plat_cfg = platforms.get(platform_key)
        if isinstance(plat_cfg, dict):
            plat_footer = plat_cfg.get("runtime_footer")
            if isinstance(plat_footer, dict):
                if "enabled" in plat_footer:
                    resolved["enabled"] = bool(plat_footer.get("enabled"))
                if isinstance(plat_footer.get("fields"), list) and plat_footer["fields"]:
                    resolved["fields"] = [str(f) for f in plat_footer["fields"]]

    return resolved


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    token_detail_estimated: bool = False,
    fields: Iterable[str] = _DEFAULT_FIELDS,
) -> str:
    """Render the footer line, or return "" if no fields have data.

    Fields are skipped silently when their underlying data is missing — a
    partially-populated footer is better than a line with ``?%`` or empty slots.
    """
    parts: list[str] = []
    for field in fields:
        if field == "model":
            m = _model_short(model)
            if m:
                parts.append(m)
        elif field == "context_pct":
            if context_length and context_length > 0 and context_tokens >= 0:
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "cwd":
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
            if rel:
                parts.append(rel)
        elif field == "token_detail":
            detail = format_context_usage_footer(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                context_length=context_length,
                estimated=token_detail_estimated,
            )
            if detail:
                parts.append(detail)
        # Unknown field names are silently ignored.

    if not parts:
        return ""
    return _SEP.join(parts)


def build_footer_line(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    token_detail_estimated: bool = False,
) -> str:
    """Top-level entry point used by gateway/run.py.

    Returns the footer text (empty string when disabled or no data).  Callers
    append this to the final response themselves, preserving a single blank
    line of separation.
    """
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_detail_estimated=token_detail_estimated,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
    )
