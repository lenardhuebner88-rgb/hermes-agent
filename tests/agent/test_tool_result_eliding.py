"""Tests for the deterministic stale-tool-result eliding pass.

Covers PlanSpec subtask WORKER-CONTEXT-DIET-ELIDE-S1:
  * AC-1 mechanism: large old read_file/skill_view bodies are trimmed, with a
    measurable before/after input-token reduction.
  * AC-2 guardrail: pure (no input mutation), youngest N turns intact, only
    read-type tools touched, pointer preserved, message schema preserved.
"""
from __future__ import annotations

import copy

import pytest

from agent.model_metadata import estimate_messages_tokens_rough
from agent.tool_result_eliding import (
    DEFAULT_MIN_ELIDE_CHARS,
    ElideConfig,
    elide_stale_tool_results,
    tool_eliding_config,
)

_BIG = "X" * (DEFAULT_MIN_ELIDE_CHARS + 5000)  # comfortably over the threshold


def _assistant(call_id, name, args):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
        ],
    }


def _tool(call_id, name, content):
    return {"role": "tool", "name": name, "tool_name": name, "content": content, "tool_call_id": call_id}


def _pair(call_id, name, args, content):
    """An assistant tool_call followed by its tool result."""
    return [_assistant(call_id, name, args), _tool(call_id, name, content)]


def _padding(n):
    """n cheap user/assistant turns to push earlier messages out of the tail."""
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"u{i}"})
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


# --------------------------------------------------------------------------
# Core eliding behaviour (AC-1 mechanism)
# --------------------------------------------------------------------------

def test_elides_large_old_read_file():
    messages = _pair("c1", "read_file", '{"path":"/repo/foo.py","offset":1}', _BIG) + _padding(20)
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 1
    assert saved > 5000
    # The big body is gone; an informative pointer remains.
    new_tool = out[1]
    assert new_tool["content"] != _BIG
    assert "[read_file]" in new_tool["content"]
    assert "/repo/foo.py" in new_tool["content"]


def test_elides_old_skill_view():
    messages = _pair("c1", "skill_view", '{"name":"deep-research"}', _BIG) + _padding(20)
    out, elided, _ = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 1
    assert "[skill_view]" in out[1]["content"]
    assert "deep-research" in out[1]["content"]


def test_does_not_elide_recent_read_file_within_tail():
    # The read result sits inside the protected tail -> untouched.
    messages = _padding(5) + _pair("c1", "read_file", '{"path":"/repo/foo.py"}', _BIG)
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 0
    assert saved == 0
    assert out[-1]["content"] == _BIG


def test_does_not_elide_small_old_read_file():
    small = "short file body"
    messages = _pair("c1", "read_file", '{"path":"/repo/foo.py"}', small) + _padding(20)
    out, elided, _ = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 0
    assert out[1]["content"] == small


def test_does_not_elide_non_read_tools():
    # terminal / write_file / search_files must never be touched, even old+large.
    messages = (
        _pair("c1", "terminal", '{"command":"npm test"}', _BIG)
        + _pair("c2", "write_file", '{"path":"/x"}', _BIG)
        + _pair("c3", "search_files", '{"pattern":"x"}', _BIG)
        + _padding(20)
    )
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 0
    assert saved == 0
    assert out[1]["content"] == _BIG
    assert out[3]["content"] == _BIG
    assert out[5]["content"] == _BIG


def test_custom_elidable_tool_set():
    messages = _pair("c1", "terminal", '{"command":"x"}', _BIG) + _padding(20)
    out, elided, _ = elide_stale_tool_results(
        messages, protect_last_n=14, elidable_tools=frozenset({"terminal"})
    )
    assert elided == 1
    assert "[terminal]" in out[1]["content"]


# --------------------------------------------------------------------------
# Correctness guardrail (AC-2)
# --------------------------------------------------------------------------

def test_pure_does_not_mutate_input():
    messages = _pair("c1", "read_file", '{"path":"/repo/foo.py"}', _BIG) + _padding(20)
    snapshot = copy.deepcopy(messages)
    out, elided, _ = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 1
    # Input is byte-for-byte unchanged; the elided result is a separate object.
    assert messages == snapshot
    assert out is not messages
    assert out[1] is not messages[1]
    assert messages[1]["content"] == _BIG


def test_message_schema_preserved():
    messages = _pair("c1", "read_file", '{"path":"/repo/foo.py"}', _BIG) + _padding(20)
    out, _, _ = elide_stale_tool_results(messages, protect_last_n=14)
    elided = out[1]
    assert elided["role"] == "tool"
    assert elided["name"] == "read_file"
    assert elided["tool_name"] == "read_file"
    assert elided["tool_call_id"] == "c1"


def test_protect_all_when_tail_covers_everything():
    messages = _pair("c1", "read_file", '{"path":"/x"}', _BIG)
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=999)
    assert (elided, saved) == (0, 0)
    assert out[1]["content"] == _BIG
    assert out is not messages  # still a fresh copy


def test_negative_protect_clamped_not_widened():
    # A garbage negative knob must not become "protect nothing / elide all the
    # way to the end" in a way that strips the live tail; clamp to >= 0.
    messages = _pair("c1", "read_file", '{"path":"/x"}', _BIG) + _padding(2)
    out, elided, _ = elide_stale_tool_results(messages, protect_last_n=-5)
    # protect_last_n clamped to 0 -> boundary == len -> the read at index 1 is
    # elidable (it is not in any tail). This documents the clamp behaviour.
    assert elided == 1


def test_non_string_content_passthrough():
    multimodal = [{"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": "data:..."}}]
    messages = [
        _assistant("c1", "read_file", '{"path":"/x"}'),
        {"role": "tool", "name": "read_file", "tool_name": "read_file", "content": multimodal, "tool_call_id": "c1"},
    ] + _padding(20)
    out, elided, _ = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 0
    assert out[1]["content"] == multimodal


def test_empty_messages():
    out, elided, saved = elide_stale_tool_results([], protect_last_n=14)
    assert out == []
    assert (elided, saved) == (0, 0)


def test_summary_uses_path_from_assistant_tool_call_args():
    messages = _pair("c1", "read_file", '{"path":"/deep/nested/module.py","offset":42}', _BIG) + _padding(20)
    out, _, _ = elide_stale_tool_results(messages, protect_last_n=14)
    assert "/deep/nested/module.py" in out[1]["content"]
    assert "42" in out[1]["content"]


def test_idempotent_already_short_summary_left_alone():
    # A body that is already a short summary (< min_elide_chars) stays put.
    summary = "[read_file] read /repo/foo.py from line 1 (12,345 chars)"
    messages = [
        _assistant("c1", "read_file", '{"path":"/repo/foo.py"}'),
        _tool("c1", "read_file", summary),
    ] + _padding(20)
    out, elided, _ = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 0
    assert out[1]["content"] == summary


def test_multiple_old_reads_all_elided_recent_kept():
    messages = (
        _pair("c1", "read_file", '{"path":"/a.py"}', _BIG)
        + _pair("c2", "read_file", '{"path":"/b.py"}', _BIG)
        + _padding(20)
        + _pair("c3", "read_file", '{"path":"/c.py"}', _BIG)  # recent -> kept
    )
    out, elided, _ = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 2
    assert out[1]["content"] != _BIG
    assert out[3]["content"] != _BIG
    assert out[-1]["content"] == _BIG  # recent read survives


# --------------------------------------------------------------------------
# AC-1 measurable before/after (deterministic synthetic Codex-style history)
# --------------------------------------------------------------------------

def test_before_after_input_tokens_drop_substantially():
    """A Codex-style history of many large old reads must shrink measurably."""
    history = []
    for i in range(12):
        history += _pair(f"c{i}", "read_file", f'{{"path":"/repo/mod_{i}.py"}}', "Y" * 8000)
    # A realistic recent working tail that must stay verbatim.
    tail = _padding(4) + _pair("clast", "read_file", '{"path":"/repo/active.py"}', "Z" * 8000)
    messages = history + tail

    before = estimate_messages_tokens_rough(messages)
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=14)
    after = estimate_messages_tokens_rough(out)

    assert elided >= 10  # the 12 old reads (recent one protected)
    assert saved > 50_000
    # Substantial reduction in carried input tokens.
    assert after < before * 0.5
    # The recent active read is preserved verbatim.
    assert out[-1]["content"] == "Z" * 8000


# --------------------------------------------------------------------------
# Config / kill-switch
# --------------------------------------------------------------------------

def test_config_defaults_enabled():
    cfg = tool_eliding_config(env={})
    assert isinstance(cfg, ElideConfig)
    assert cfg.enabled is True
    assert cfg.protect_last_n == 14
    assert cfg.min_elide_chars == DEFAULT_MIN_ELIDE_CHARS
    assert "read_file" in cfg.elidable_tools and "skill_view" in cfg.elidable_tools


def test_config_kill_switch():
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_DISABLED": "1"}).enabled is False
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_DISABLED": "true"}).enabled is False
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_DISABLED": "0"}).enabled is True


def test_config_env_tunables():
    cfg = tool_eliding_config(env={"HERMES_TOOL_ELIDE_PROTECT_N": "20", "HERMES_TOOL_ELIDE_MIN_CHARS": "3000"})
    assert cfg.protect_last_n == 20
    assert cfg.min_elide_chars == 3000


def test_config_bad_env_falls_back_to_defaults():
    cfg = tool_eliding_config(env={"HERMES_TOOL_ELIDE_PROTECT_N": "abc", "HERMES_TOOL_ELIDE_MIN_CHARS": "-1"})
    assert cfg.protect_last_n == 14
    assert cfg.min_elide_chars == DEFAULT_MIN_ELIDE_CHARS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
