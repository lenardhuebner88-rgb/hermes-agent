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

from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


def _safe_nonnegative_int(value: Any) -> int:
    """Return *value* as a non-negative int, or 0 when missing/invalid."""
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _format_token_count(value: int | None) -> str:
    """Compact token counts for footer display (1200 → 1.2k)."""
    if value is None:
        return ""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return ""
    if count < 0:
        return ""
    if count < 1_000:
        return str(count)

    def _compact(divisor: int, suffix: str) -> str:
        scaled = Decimal(count) / Decimal(divisor)
        if scaled >= Decimal("100") or scaled == scaled.to_integral_value():
            text = str(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            text = str(scaled.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
            text = text.rstrip("0").rstrip(".")
        return f"{text}{suffix}"

    if count < 999_500:
        return _compact(1_000, "k")
    return _compact(1_000_000, "M")


def _context_percent(context_tokens: int, context_length: Optional[int]) -> Optional[int]:
    """Return clamped context-window percentage, or None for invalid input."""
    if not context_length or context_length <= 0 or context_tokens < 0:
        return None
    return max(0, min(100, round((context_tokens / context_length) * 100)))


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
    fields: Iterable[str] = _DEFAULT_FIELDS,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    cache_write_tokens: int | None = 0,
    reasoning_tokens: int | None = 0,
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
            pct = _context_percent(context_tokens, context_length)
            if pct is not None:
                parts.append(f"{pct}%")
        elif field == "context_detail":
            pct = _context_percent(context_tokens, context_length)
            if pct is not None:
                ctx = _format_token_count(context_tokens)
                window = _format_token_count(context_length)
                if ctx and window:
                    parts.append(f"ctx {pct}% ({ctx}/{window})")
                else:
                    parts.append(f"ctx {pct}%")
        elif field == "token_detail":
            in_toks = _safe_nonnegative_int(input_tokens)
            out_toks = _safe_nonnegative_int(output_tokens)
            cache_read = _safe_nonnegative_int(cache_read_tokens)
            cache_write = _safe_nonnegative_int(cache_write_tokens)
            reasoning = _safe_nonnegative_int(reasoning_tokens)

            token_parts: list[str] = []
            if in_toks or cache_read or cache_write:
                input_piece = f"in {_format_token_count(in_toks)}"
                if cache_read:
                    input_piece += f" + cache {_format_token_count(cache_read)}"
                if cache_write:
                    input_piece += f" + write {_format_token_count(cache_write)}"
                token_parts.append(input_piece)
            if out_toks:
                token_parts.append(f"out {_format_token_count(out_toks)}")
            if reasoning:
                token_parts.append(f"reason {_format_token_count(reasoning)}")
            if token_parts:
                parts.append(_SEP.join(token_parts))
        elif field == "cwd":
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
            if rel:
                parts.append(rel)
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
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    cache_write_tokens: int | None = 0,
    reasoning_tokens: int | None = 0,
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
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
    )
