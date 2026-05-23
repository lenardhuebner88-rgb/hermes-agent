"""Unit tests for gateway.runtime_footer — the opt-in runtime-metadata footer
appended to final gateway replies."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from agent.usage_pricing import normalize_usage
from gateway.platforms.base import BasePlatformAdapter
from gateway.runtime_footer import (
    _home_relative_cwd,
    _model_short,
    build_footer_line,
    format_context_usage_footer,
    format_runtime_footer,
    resolve_token_detail_usage,
    resolve_footer_config,
)


# ---------------------------------------------------------------------------
# _model_short + _home_relative_cwd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.4", "gpt-5.4"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("gpt-5.4", "gpt-5.4"),
        ("", ""),
        (None, ""),
    ],
)
def test_model_short_drops_vendor_prefix(model, expected):
    assert _model_short(model) == expected


def test_home_relative_cwd_collapses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sub = tmp_path / "projects" / "hermes"
    sub.mkdir(parents=True)
    result = _home_relative_cwd(str(sub))
    assert result == "~/projects/hermes"


def test_home_relative_cwd_leaves_abs_path_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    result = _home_relative_cwd(str(tmp_path / "outside" / "dir"))
    assert result == str(tmp_path / "outside" / "dir")


def test_home_relative_cwd_empty_returns_empty():
    assert _home_relative_cwd("") == ""


# ---------------------------------------------------------------------------
# format_runtime_footer
# ---------------------------------------------------------------------------

def test_format_footer_all_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "projects" / "hermes"))
    (tmp_path / "projects" / "hermes").mkdir(parents=True)
    out = format_runtime_footer(
        model="openrouter/openai/gpt-5.4",
        context_tokens=68000,
        context_length=100000,
        cwd=None,  # falls back to TERMINAL_CWD env var
        fields=("model", "context_pct", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · ~/projects/hermes"


def test_format_footer_skips_missing_context_length():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=500,
        context_length=None,
        cwd="/tmp/wd",
        fields=("model", "context_pct", "cwd"),
    )
    # context_pct dropped silently; no "?%" artifact
    assert "%" not in out
    assert "gpt-5.4" in out
    assert "/tmp/wd" in out


def test_format_footer_context_pct_clamped_to_100():
    out = format_runtime_footer(
        model="m",
        context_tokens=500_000,  # way over
        context_length=100_000,
        cwd="",
        fields=("context_pct",),
    )
    assert out == "100%"


def test_format_footer_context_pct_never_negative():
    out = format_runtime_footer(
        model="m",
        context_tokens=-50,
        context_length=100,
        cwd="",
        fields=("context_pct",),
    )
    # Negative input => no field emitted (we require context_tokens >= 0)
    assert out == ""


def test_format_footer_empty_fields_returns_empty():
    out = format_runtime_footer(
        model="m", context_tokens=0, context_length=100,
        cwd="/x", fields=(),
    )
    assert out == ""


def test_format_footer_drops_cwd_when_empty(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="",
        fields=("model", "context_pct", "cwd"),
    )
    # cwd silently dropped; model + pct remain
    assert out == "gpt-5.4 · 50%"


def test_format_footer_custom_field_order():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="/opt/project",
        fields=("context_pct", "model"),  # swapped + no cwd
    )
    assert out == "50% · gpt-5.4"


def test_format_footer_unknown_field_silently_ignored():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="/x",
        fields=("model", "bogus", "context_pct"),
    )
    assert out == "gpt-5.4 · 50%"


# ---------------------------------------------------------------------------
# resolve_footer_config
# ---------------------------------------------------------------------------

def test_resolve_defaults_off_empty_config():
    cfg = resolve_footer_config({}, "telegram")
    assert cfg == {"enabled": False, "fields": ["model", "context_pct", "cwd"]}


def test_resolve_global_enable():
    user = {"display": {"runtime_footer": {"enabled": True}}}
    cfg = resolve_footer_config(user, "telegram")
    assert cfg["enabled"] is True
    assert cfg["fields"] == ["model", "context_pct", "cwd"]


def test_resolve_platform_override_wins():
    user = {
        "display": {
            "runtime_footer": {"enabled": True, "fields": ["model"]},
            "platforms": {
                "slack": {"runtime_footer": {"enabled": False}},
            },
        },
    }
    # Telegram picks up the global enable
    assert resolve_footer_config(user, "telegram")["enabled"] is True
    # Slack overrides to off
    assert resolve_footer_config(user, "slack")["enabled"] is False


def test_resolve_platform_can_add_fields_only():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {
                "discord": {"runtime_footer": {"fields": ["context_pct"]}},
            },
        },
    }
    tg = resolve_footer_config(user, "telegram")
    assert tg["enabled"] is True
    assert tg["fields"] == ["model", "context_pct", "cwd"]
    dc = resolve_footer_config(user, "discord")
    assert dc["enabled"] is True
    assert dc["fields"] == ["context_pct"]


def test_resolve_ignores_malformed_config():
    # Non-dict runtime_footer shouldn't crash
    user = {"display": {"runtime_footer": "on"}}
    cfg = resolve_footer_config(user, "telegram")
    assert cfg["enabled"] is False


# ---------------------------------------------------------------------------
# build_footer_line — top-level entry point used by gateway/run.py
# ---------------------------------------------------------------------------

def test_build_footer_empty_when_disabled():
    out = build_footer_line(
        user_config={},
        platform_key="telegram",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""


def test_build_footer_returns_rendered_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="telegram",
        model="openai/gpt-5.4",
        context_tokens=25, context_length=100,
        cwd=str(tmp_path / "proj"),
    )
    (tmp_path / "proj").mkdir(exist_ok=True)
    assert "gpt-5.4" in out
    assert "25%" in out


def test_build_footer_per_platform_off_suppresses():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {"slack": {"runtime_footer": {"enabled": False}}},
        },
    }
    out = build_footer_line(
        user_config=user,
        platform_key="slack",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""


def test_build_footer_no_data_returns_empty_even_when_enabled():
    # Enabled, but context_length is None AND cwd empty AND model empty ⇒ no fields
    out = build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="telegram",
        model="",
        context_tokens=0, context_length=None,
        cwd="",
    )
    # With no TERMINAL_CWD env either
    if not os.environ.get("TERMINAL_CWD"):
        assert out == ""


# ---------------------------------------------------------------------------
# token_detail / context usage footer
# ---------------------------------------------------------------------------


def test_format_context_usage_footer_exact_values():
    assert format_context_usage_footer(
        input_tokens=14_800,
        output_tokens=312,
        context_length=200_000,
    ) == "Kontext: 7 % · 14.8k/200k Token · Antwort: 312"


@pytest.mark.parametrize(
    "input_tokens,output_tokens,context_length",
    [
        (14_800, 312, None),
        (14_800, 312, 0),
        (None, 312, 200_000),
        (14_800, None, 200_000),
        (-1, 312, 200_000),
    ],
)
def test_format_context_usage_footer_missing_or_invalid_returns_none(
    input_tokens, output_tokens, context_length,
):
    assert format_context_usage_footer(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        context_length=context_length,
    ) is None


def test_format_context_usage_footer_estimated_prefixes_estimated_values():
    out = format_context_usage_footer(
        input_tokens=14_800,
        output_tokens=312,
        context_length=200_000,
        estimated=True,
    )
    assert out == "Kontext: ~7 % · ~14.8k/200k Token · Antwort: 312"


def test_resolve_token_detail_usage_prefers_last_prompt_tokens():
    input_tokens, output_tokens, estimated = resolve_token_detail_usage({
        "last_prompt_tokens": 12_000,
        "last_input_tokens": 14_800,
        "last_output_tokens": 312,
        "input_tokens": 99_999,
        "output_tokens": 9_999,
    })

    assert (input_tokens, output_tokens, estimated) == (12_000, 312, False)


def test_resolve_token_detail_usage_uses_legacy_last_input_when_prompt_missing():
    input_tokens, output_tokens, estimated = resolve_token_detail_usage({
        "last_input_tokens": 14_800,
        "last_output_tokens": 312,
        "input_tokens": 99_999,
        "output_tokens": 9_999,
    })

    assert (input_tokens, output_tokens, estimated) == (14_800, 312, False)


def test_resolve_token_detail_usage_does_not_use_aggregate_input_tokens():
    input_tokens, output_tokens, estimated = resolve_token_detail_usage({
        "last_input_tokens": None,
        "last_output_tokens": None,
        "input_tokens": 64_000,
        "output_tokens": 1_234,
    })

    assert (input_tokens, output_tokens, estimated) == (None, 1_234, True)


def test_build_footer_token_detail_uses_last_prompt_with_output_fallback_as_estimated():
    input_tokens, output_tokens, estimated = resolve_token_detail_usage({
        "last_prompt_tokens": 32_000,
        "input_tokens": 64_000,
        "output_tokens": 1_234,
    })

    out = build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True, "fields": ["model", "token_detail"]}}},
        platform_key="discord",
        model="openai/gpt-5.5",
        context_tokens=32_000,
        context_length=200_000,
        cwd="",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_detail_estimated=estimated,
    )

    assert out == "gpt-5.5 · Kontext: ~16 % · ~32k/200k Token · Antwort: 1234"


def test_token_detail_uses_codex_responses_normalized_effective_input_tokens():
    raw_usage = SimpleNamespace(
        input_tokens=19_000,
        output_tokens=312,
        input_tokens_details=SimpleNamespace(cached_tokens=4_200),
    )
    usage = normalize_usage(raw_usage, provider="openai-codex", api_mode="codex_responses")

    out = format_context_usage_footer(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        context_length=200_000,
    )

    assert usage.input_tokens == 14_800
    assert usage.prompt_tokens == 19_000
    assert out == "Kontext: 7 % · 14.8k/200k Token · Antwort: 312"


def test_format_runtime_footer_token_detail_combines_with_existing_fields():
    out = format_runtime_footer(
        model="openai/gpt-5.5",
        context_tokens=14_800,
        context_length=200_000,
        cwd="",
        input_tokens=14_800,
        output_tokens=312,
        fields=("model", "context_pct", "token_detail"),
    )

    assert out == "gpt-5.5 · 7% · Kontext: 7 % · 14.8k/200k Token · Antwort: 312"


def test_build_footer_token_detail_opt_in_only():
    base = {
        "platform_key": "discord",
        "model": "openai/gpt-5.5",
        "context_tokens": 14_800,
        "context_length": 200_000,
        "cwd": "",
        "input_tokens": 14_800,
        "output_tokens": 312,
    }

    assert build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": False, "fields": ["token_detail"]}}},
        **base,
    ) == ""
    assert build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True, "fields": ["token_detail"]}}},
        **base,
    ) == "Kontext: 7 % · 14.8k/200k Token · Antwort: 312"
    assert build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True, "fields": ["token_detail"]}}},
        **{**base, "input_tokens": None},
    ) == ""


def test_footer_appended_message_splits_with_token_detail_in_final_chunk():
    footer = format_context_usage_footer(
        input_tokens=14_800,
        output_tokens=312,
        context_length=200_000,
    )
    message = ("x" * 1980) + "\n\n" + footer
    chunks = BasePlatformAdapter.truncate_message(message, max_length=2000)

    assert chunks[-1].endswith(f"{footer} ({len(chunks)}/{len(chunks)})")
    assert all(len(chunk) <= 2000 for chunk in chunks)
