"""Characterization tests for pure helpers in ``agent.message_sanitization``.

Pins the observable behavior of two private, side-effect-bounded helpers ahead
of the R3/R4 refactors:

* ``_sanitize_messages_surrogates`` — in-place replacement of lone surrogate
  code points (``\\ud800``-``\\udfff``) with U+FFFD across message string fields
  (R3 touches the nested-if in the additional-fields walk).
* ``_repair_tool_call_arguments`` — best-effort repair of malformed tool-call
  JSON, returning ``"{}"`` as a last resort (R4 touches the excess-closer
  removal loop).

Surrogate / replacement characters are written as ``\\uXXXX`` escape sequences
(pure ASCII in this file) to avoid any source-encoding ambiguity.
"""
from __future__ import annotations

from agent.message_sanitization import (
    _repair_tool_call_arguments,
    _sanitize_messages_surrogates,
)

LONE = "\ud800"  # lone surrogate (invalid in UTF-8)
REPL = "\ufffd"  # replacement character (U+FFFD)


# ─── _sanitize_messages_surrogates ──────────────────────────────────────────


def test_sanitize_replaces_surrogate_in_content_string():
    msgs = [{"role": "user", "content": f"hi{LONE}there"}]
    assert _sanitize_messages_surrogates(msgs) is True
    assert msgs[0]["content"] == f"hi{REPL}there"


def test_sanitize_replaces_surrogate_in_additional_string_field():
    # Reasoning-style fields (not content/name/tool_calls/role) are walked too.
    msgs = [{"role": "assistant", "content": "ok", "reasoning_content": f"a{LONE}b"}]
    assert _sanitize_messages_surrogates(msgs) is True
    assert msgs[0]["reasoning_content"] == f"a{REPL}b"


def test_sanitize_replaces_surrogate_in_nested_dict_field():
    msgs = [{"role": "assistant", "reasoning_details": {"text": f"x{LONE}y"}}]
    assert _sanitize_messages_surrogates(msgs) is True
    assert msgs[0]["reasoning_details"]["text"] == f"x{REPL}y"


def test_sanitize_replaces_surrogate_in_content_list_text():
    msgs = [{"role": "user", "content": [{"type": "text", "text": f"p{LONE}q"}]}]
    assert _sanitize_messages_surrogates(msgs) is True
    assert msgs[0]["content"][0]["text"] == f"p{REPL}q"


def test_sanitize_replaces_surrogate_in_tool_call_arguments():
    msgs = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "call_1",
            "function": {"name": "do", "arguments": f'{{"a":"{LONE}"}}'},
        }],
    }]
    assert _sanitize_messages_surrogates(msgs) is True
    assert msgs[0]["tool_calls"][0]["function"]["arguments"] == f'{{"a":"{REPL}"}}'


def test_sanitize_returns_false_and_unchanged_when_clean():
    msgs = [{"role": "user", "content": "clean text", "reasoning_content": "also clean"}]
    before = [dict(m) for m in msgs]
    assert _sanitize_messages_surrogates(msgs) is False
    assert msgs == before


def test_sanitize_skips_non_dict_messages():
    msgs = ["not a dict", {"role": "user", "content": f"a{LONE}b"}]
    assert _sanitize_messages_surrogates(msgs) is True
    assert msgs[0] == "not a dict"
    assert msgs[1]["content"] == f"a{REPL}b"


def test_sanitize_skips_role_field_in_additional_walk():
    # The additional-fields walk explicitly skips {content,name,tool_calls,role};
    # a surrogate in role is therefore left as-is (current behavior to preserve).
    msgs = [{"role": f"weird{LONE}role", "content": "ok"}]
    assert _sanitize_messages_surrogates(msgs) is False
    assert msgs[0]["role"] == f"weird{LONE}role"


# ─── _repair_tool_call_arguments ────────────────────────────────────────────


def test_repair_fast_path_reserialises_valid_json_compactly():
    assert _repair_tool_call_arguments('{"a": 1, "b": [2, 3]}') == '{"a":1,"b":[2,3]}'


def test_repair_empty_and_whitespace_return_empty_object():
    assert _repair_tool_call_arguments("") == "{}"
    assert _repair_tool_call_arguments("   \n ") == "{}"


def test_repair_python_none_literal_returns_empty_object():
    assert _repair_tool_call_arguments("None") == "{}"


def test_repair_strips_trailing_comma():
    assert _repair_tool_call_arguments('{"a":1,}') == '{"a":1}'


def test_repair_closes_unclosed_object():
    assert _repair_tool_call_arguments('{"a":1') == '{"a":1}'


def test_repair_removes_excess_closing_brace():
    # Exercises the `}` removal branch of the excess-closer loop.
    assert _repair_tool_call_arguments('{"a":1}}') == '{"a":1}'


def test_repair_removes_excess_closing_bracket():
    # Exercises the `]` removal branch of the excess-closer loop.
    assert _repair_tool_call_arguments('[1,2]]') == '[1,2]'


def test_repair_unrepairable_returns_empty_object():
    # No braces/brackets to balance and not JSON → falls through to last resort.
    assert _repair_tool_call_arguments("not json at all") == "{}"
