"""Characterization tests for delegate_tool error/edge-path helpers.

Three pure helpers on the delegation observability + batch-parse path:

* ``_stringify_tool_content`` — turns arbitrary tool-result content (str, list
  of content-blocks, dict, other) into stable text. Must NEVER crash while
  summarising a child run, regardless of transport shape.
* ``_looks_like_error_output`` — conservative classifier that decides whether a
  tool-result preview is an error. A false NEGATIVE makes the orchestrator
  report "all tasks succeeded" when a sub-agent actually failed → silently
  wrong final answer.
* ``_recover_tasks_from_json_string`` — parses a batch ``tasks`` argument
  emitted as a raw JSON string, returning structured guidance on failure so the
  model can self-correct instead of burning retry budget on cryptic errors.
"""
from __future__ import annotations

from tools.delegate_tool import (
    _looks_like_error_output,
    _recover_tasks_from_json_string,
    _stringify_tool_content,
)

# ─── _stringify_tool_content ─────────────────────────────────────────────────


def test_stringify_none_is_empty_string():
    assert _stringify_tool_content(None) == ""


def test_stringify_str_is_identity():
    assert _stringify_tool_content("hello") == "hello"


def test_stringify_list_of_text_blocks_joins_text():
    content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert _stringify_tool_content(content) == "a\nb"


def test_stringify_list_of_non_text_dicts_is_json_dumped():
    content = [{"type": "image", "url": "x"}]
    out = _stringify_tool_content(content)
    assert '"type": "image"' in out and '"url": "x"' in out


def test_stringify_list_of_non_dicts_is_str_each():
    assert _stringify_tool_content([1, "two"]) == "1\ntwo"


def test_stringify_dict_is_json_dumped():
    out = _stringify_tool_content({"error": "boom"})
    assert '"error": "boom"' in out


def test_stringify_other_types_fall_back_to_str():
    assert _stringify_tool_content(42) == "42"


# ─── _looks_like_error_output ────────────────────────────────────────────────


def test_empty_and_none_are_not_errors():
    assert _looks_like_error_output("") is False
    assert _looks_like_error_output(None) is False


def test_plain_text_containing_error_substring_is_not_flagged():
    # The whole point of the conservative rewrite: the substring "error" alone
    # must NOT paint normal output red.
    assert _looks_like_error_output("operation complete, no error found") is False
    assert _looks_like_error_output("errors: 0") is False  # "errors:" != "error:"


def test_first_line_error_markers_are_flagged():
    assert _looks_like_error_output("Error: boom") is True
    assert _looks_like_error_output("Failed: x") is True
    assert _looks_like_error_output("Exception: y") is True
    assert _looks_like_error_output("error: lower-case too") is True


def test_traceback_marker_requires_trailing_space():
    # CHARACTERIZATION quirk: the marker is "traceback " (with a space), so the
    # classic Python line matches but "Traceback:" / "Tracebacks" do not.
    assert _looks_like_error_output("Traceback (most recent call last):") is True
    assert _looks_like_error_output("Traceback:") is False
    assert _looks_like_error_output("Tracebacks are useful") is False


def test_json_dict_with_truthy_error_key_is_flagged():
    assert _looks_like_error_output('{"error": "boom"}') is True


def test_json_dict_with_falsy_error_key_is_not_flagged():
    # Falsy error key and no error status → not an error.
    assert _looks_like_error_output('{"error": ""}') is False
    assert _looks_like_error_output('{"error": null, "status": "ok"}') is False


def test_json_dict_error_status_is_flagged_case_insensitively():
    assert _looks_like_error_output('{"status": "FAILED"}') is True
    assert _looks_like_error_output('{"status": "failure"}') is True
    assert _looks_like_error_output('{"status": "timeout"}') is True
    assert _looks_like_error_output('{"status": "ok"}') is False


def test_json_list_is_not_flagged():
    # Parsed JSON that is a list (not dict) has no error/status key to inspect.
    assert _looks_like_error_output('[{"status": "error"}]') is False


def test_invalid_json_starting_with_brace_is_not_flagged():
    # Unparseable → the JSON branch is skipped and "{" is not a line marker.
    assert _looks_like_error_output('{"error": ') is False


def test_leading_whitespace_before_json_error_is_still_flagged():
    assert _looks_like_error_output('   {"error": "boom"}') is True


def test_dict_input_is_stringified_then_flagged():
    assert _looks_like_error_output({"error": "boom"}) is True


def test_content_block_list_with_error_text_is_flagged():
    assert _looks_like_error_output([{"type": "text", "text": "Error: x"}]) is True


# ─── _recover_tasks_from_json_string ─────────────────────────────────────────


def test_non_string_input_returns_none_none():
    # A proper list (the normal path) is not this helper's job → (None, None).
    assert _recover_tasks_from_json_string([{"goal": "x"}]) == (None, None)
    assert _recover_tasks_from_json_string(None) == (None, None)
    assert _recover_tasks_from_json_string({"goal": "x"}) == (None, None)


def test_blank_string_returns_guidance_message():
    parsed, err = _recover_tasks_from_json_string("   ")
    assert parsed is None
    assert "goal" in err and "tasks" in err


def test_invalid_json_returns_parse_guidance():
    parsed, err = _recover_tasks_from_json_string('[{"goal": "x"')  # unterminated
    assert parsed is None
    assert "could not be parsed as JSON" in err


def test_valid_json_but_not_a_list_returns_type_guidance():
    parsed, err = _recover_tasks_from_json_string('{"goal": "x"}')
    assert parsed is None
    assert "dict" in err  # reports the parsed type name


def test_valid_json_array_is_returned():
    tasks = [{"goal": "a"}, {"goal": "b"}]
    parsed, err = _recover_tasks_from_json_string('[{"goal": "a"}, {"goal": "b"}]')
    assert err is None
    assert parsed == tasks


def test_empty_json_array_is_a_valid_list():
    parsed, err = _recover_tasks_from_json_string("[]")
    assert err is None
    assert parsed == []


def test_surrounding_whitespace_is_stripped_before_parse():
    parsed, err = _recover_tasks_from_json_string('  [{"goal": "a"}]  ')
    assert err is None
    assert parsed == [{"goal": "a"}]
