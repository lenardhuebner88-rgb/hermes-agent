"""Tests for the deterministic stale-tool-result eliding pass.

Covers PlanSpec subtask WORKER-CONTEXT-DIET-ELIDE-S1:
  * AC-1 mechanism: large old read_file/skill_view/search_files/terminal bodies
    are trimmed, with a measurable before/after input-token reduction.
  * AC-2 guardrail: pure (no input mutation), youngest N turns intact, only
    bounded tool classes touched, pointer preserved, message schema preserved.
"""
from __future__ import annotations

import copy

import pytest

from agent.model_metadata import estimate_messages_tokens_rough
from agent.tool_result_eliding import (
    DEFAULT_CACHE_QUANTIZE_STEP,
    DEFAULT_MIN_ELIDE_CHARS,
    ElideConfig,
    cache_stable_boundary,
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


def test_elides_old_search_files_and_terminal_by_default():
    messages = (
        _pair("c1", "search_files", '{"pattern":"DEFAULT_ELIDABLE_TOOLS"}', _BIG)
        + _pair("c2", "terminal", '{"command":"scripts/run_tests.sh tests/agent/test_tool_result_eliding.py"}', _BIG)
        + _padding(20)
    )
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 2
    assert saved > 10_000
    assert "[search_files]" in out[1]["content"]
    assert "DEFAULT_ELIDABLE_TOOLS" in out[1]["content"]
    assert "[terminal]" in out[3]["content"]
    assert "scripts/run_tests.sh" in out[3]["content"]


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


def test_does_not_elide_edit_tools_by_default():
    # Mutating edit results stay verbatim even old+large.
    messages = (
        _pair("c1", "write_file", '{"path":"/x"}', _BIG)
        + _pair("c2", "patch", '{"path":"/x"}', _BIG)
        + _padding(20)
    )
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=14)
    assert elided == 0
    assert saved == 0
    assert out[1]["content"] == _BIG
    assert out[3]["content"] == _BIG


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
    history += _pair("c-search", "search_files", '{"pattern":"expensive"}', "S" * 8000)
    history += _pair("c-term", "terminal", '{"command":"scripts/run-affected.sh"}', "T" * 8000)
    # A realistic recent working tail that must stay verbatim.
    tail = _padding(4) + _pair("clast", "read_file", '{"path":"/repo/active.py"}', "Z" * 8000)
    messages = history + tail

    before = estimate_messages_tokens_rough(messages)
    out, elided, saved = elide_stale_tool_results(messages, protect_last_n=14)
    after = estimate_messages_tokens_rough(out)

    assert elided >= 12  # old reads plus old search/terminal; recent one protected
    assert saved > 65_000
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
    assert cfg.elidable_tools == frozenset({"read_file", "skill_view", "search_files", "terminal"})


def test_config_kill_switch():
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_DISABLED": "1"}).enabled is False
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_DISABLED": "true"}).enabled is False
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_DISABLED": "0"}).enabled is True


def test_config_env_tunables():
    cfg = tool_eliding_config(env={"HERMES_TOOL_ELIDE_PROTECT_N": "20", "HERMES_TOOL_ELIDE_MIN_CHARS": "3000"})
    assert cfg.protect_last_n == 20
    assert cfg.min_elide_chars == 3000


def test_config_tool_allowlist_override_is_bounded():
    cfg = tool_eliding_config(
        env={"HERMES_TOOL_ELIDE_TOOLS": "read_file, skill_view, patch, session_search, unknown"}
    )
    assert cfg.elidable_tools == frozenset({"read_file", "skill_view", "session_search"})


def test_config_bad_env_falls_back_to_defaults():
    cfg = tool_eliding_config(env={"HERMES_TOOL_ELIDE_PROTECT_N": "abc", "HERMES_TOOL_ELIDE_MIN_CHARS": "-1"})
    assert cfg.protect_last_n == 14
    assert cfg.min_elide_chars == DEFAULT_MIN_ELIDE_CHARS


# --------------------------------------------------------------------------
# Cache-aware eliding for prompt-cached Claude lanes
# (PlanSpec subtask CLAUDE-LANE-CACHEAWARE-ELIDE-S1)
#
# The danger the quantized boundary defends against: the end-relative boundary
# (n - protect_last_n) slides forward ~2 messages every turn, so naive eliding
# mutates a NEW tool body deep inside the Anthropic stable cache prefix on every
# turn -> the cache diverges at that byte each turn (cache-read 0.1x -> cache-
# write 1.25x), which can RAISE real cost. Snapping the boundary down to a
# multiple of `step` keeps the elided prefix byte-identical across consecutive
# turns, so the cache keeps hitting and only the carried payload shrinks.
# --------------------------------------------------------------------------


def test_cache_stable_boundary_quantizes_down_to_step_multiple():
    # raw = 40 - 14 = 26 -> snapped down to nearest multiple of 8 = 24.
    assert cache_stable_boundary(40, protect_last_n=14, quantize_step=8) == 24


def test_cache_stable_boundary_is_stable_across_a_turn_append():
    # The core cache-safety property: appending messages within the same step
    # block must NOT move the boundary (so the elided prefix stays byte-stable).
    b40 = cache_stable_boundary(40, protect_last_n=14, quantize_step=8)
    b41 = cache_stable_boundary(41, protect_last_n=14, quantize_step=8)
    b42 = cache_stable_boundary(42, protect_last_n=14, quantize_step=8)
    assert b40 == b41 == b42 == 24


def test_cache_stable_boundary_advances_by_one_block_per_step():
    # raw crosses 32 at n=46 -> boundary jumps from 24 to 32 (exactly one block).
    assert cache_stable_boundary(45, protect_last_n=14, quantize_step=8) == 24
    assert cache_stable_boundary(46, protect_last_n=14, quantize_step=8) == 32


def test_cache_stable_boundary_short_convo_elides_nothing():
    # raw = 20 - 14 = 6 < step -> 0: short conversations elide nothing at all
    # (exactly when naive eliding would be net-negative on a cache lane).
    assert cache_stable_boundary(20, protect_last_n=14, quantize_step=8) == 0


def test_cache_stable_boundary_step_zero_is_raw_boundary():
    # Backward-compat: step 0 (or 1) reproduces the plain end-relative boundary.
    assert cache_stable_boundary(40, protect_last_n=14, quantize_step=0) == 26
    assert cache_stable_boundary(40, protect_last_n=14, quantize_step=1) == 26


def test_cache_stable_boundary_clamps_negative_protect():
    # A garbage negative protect knob is clamped to 0, never widened.
    assert cache_stable_boundary(40, protect_last_n=-5, quantize_step=8) == 40


def test_quantize_never_exceeds_naive_elided_set():
    # The quantized elided set must always be a SUBSET of the naive one: it never
    # touches a message the naive pass would protect, so the protected tail and
    # everything newer than the snapped boundary stay verbatim.
    messages = _convo_reads(20) + _padding(7)  # 40 + 14 = 54 messages
    naive, naive_n, _ = elide_stale_tool_results(messages, protect_last_n=14, quantize_step=0)
    quant, quant_n, _ = elide_stale_tool_results(messages, protect_last_n=14, quantize_step=8)
    assert quant_n <= naive_n
    # Every index quantize elided is also elided by naive (subset, identical bytes).
    for idx, (qm, nm) in enumerate(zip(quant, naive)):
        if qm.get("role") == "tool" and qm.get("content") != messages[idx].get("content"):
            assert nm.get("content") == qm.get("content"), idx


def test_quantized_elided_prefix_is_byte_stable_across_append():
    # AC-2 cache guarantee: eliding the same conversation at length L and L+2
    # (an extra turn appended, same step block) yields a byte-IDENTICAL elided
    # prefix -> the Anthropic stable-prefix cache keeps hitting.
    base = _convo_reads(20)                    # 40 messages, boundary snaps to 24
    later = base + _pair("cN", "read_file", '{"path":"/repo/new.py"}', _BIG)  # +2 -> 42
    out_base, _, _ = elide_stale_tool_results(base, protect_last_n=14, quantize_step=8)
    out_later, _, _ = elide_stale_tool_results(later, protect_last_n=14, quantize_step=8)
    boundary = cache_stable_boundary(len(base), protect_last_n=14, quantize_step=8)
    assert boundary == 24
    # The elided prefix [0, boundary) is identical in both turns, byte-for-byte.
    for idx in range(boundary):
        assert out_base[idx] == out_later[idx], idx


def test_quantize_protects_partial_trailing_block():
    # _convo_reads puts the assistant tool_call at even indices and the read
    # RESULT (tool message) at odd indices. With raw boundary 26 and step 8 the
    # snapped boundary is 24, so the read result at index 25 sits in the partial
    # trailing block [24, 26) that naive WOULD elide but quantize protects.
    messages = _convo_reads(20)  # 40 messages; tool results at odd indices 1..39
    naive, _, _ = elide_stale_tool_results(messages, protect_last_n=14, quantize_step=0)
    out, _, _ = elide_stale_tool_results(messages, protect_last_n=14, quantize_step=8)
    # Naive elides the read result at index 25; quantize keeps it verbatim.
    assert naive[25]["content"] != _BIG
    assert out[25]["content"] == _BIG
    # A read result inside the [0, 24) block is elided under quantization.
    assert out[23]["content"] != _BIG
    assert "[read_file]" in out[23]["content"]


def test_quantize_step_does_not_mutate_input():
    messages = _convo_reads(20)
    snapshot = copy.deepcopy(messages)
    elide_stale_tool_results(messages, protect_last_n=14, quantize_step=8)
    assert messages == snapshot


def test_cache_aware_token_reduction_meets_ac1_target():
    """AC-1 mechanism on a cache lane: a long Claude-style history's carried
    tokens must drop by >=20% even under the cache-stable quantized boundary."""
    history = []
    for i in range(24):
        history += _pair(f"c{i}", "read_file", f'{{"path":"/repo/mod_{i}.py"}}', "Y" * 8000)
    tail = _padding(4) + _pair("clast", "read_file", '{"path":"/repo/active.py"}', "Z" * 8000)
    messages = history + tail  # 48 + 8 + 2 = 58 messages

    before = estimate_messages_tokens_rough(messages)
    out, elided, saved = elide_stale_tool_results(
        messages, protect_last_n=14, quantize_step=8
    )
    after = estimate_messages_tokens_rough(out)

    assert elided >= 10
    # >= 20% reduction in carried input tokens (AC-1 "mindestens 20%").
    assert after <= before * 0.80
    # Recent active read preserved verbatim (cache-protected working tail).
    assert out[-1]["content"] == "Z" * 8000


# --------------------------------------------------------------------------
# Cache-aware config (extends the existing HERMES_TOOL_ELIDE_* schema)
# --------------------------------------------------------------------------


def test_config_cache_aware_defaults_enabled():
    cfg = tool_eliding_config(env={})
    assert cfg.cache_aware_enabled is True
    assert cfg.cache_quantize_step == DEFAULT_CACHE_QUANTIZE_STEP


def test_config_cache_aware_kill_switch():
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_CACHE_AWARE_DISABLED": "1"}).cache_aware_enabled is False
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_CACHE_AWARE_DISABLED": "true"}).cache_aware_enabled is False
    assert tool_eliding_config(env={"HERMES_TOOL_ELIDE_CACHE_AWARE_DISABLED": "0"}).cache_aware_enabled is True


def test_config_cache_step_tunable():
    cfg = tool_eliding_config(env={"HERMES_TOOL_ELIDE_CACHE_STEP": "16"})
    assert cfg.cache_quantize_step == 16


def test_config_cache_step_bad_env_falls_back():
    cfg = tool_eliding_config(env={"HERMES_TOOL_ELIDE_CACHE_STEP": "garbage"})
    assert cfg.cache_quantize_step == DEFAULT_CACHE_QUANTIZE_STEP


def _convo_reads(num_pairs):
    """num_pairs assistant->read_file pairs of big bodies (2 messages each)."""
    out = []
    for i in range(num_pairs):
        out += _pair(f"c{i}", "read_file", f'{{"path":"/repo/mod_{i}.py"}}', _BIG)
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
