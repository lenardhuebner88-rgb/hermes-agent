"""Characterization tests for AIAgent error-detail display coercion.

Two pure(ish) helpers that turn raw/structured provider errors into display-safe
one-liners. Silent regressions here either dump a 60 KB HTML error page verbatim
to the user/messaging adapter, or leak/crash on a structured error body.

* ``_clean_error_message`` — user-facing cleanup: HTML pages → a fixed friendly
  string, whitespace collapsed, truncated to 150 chars.
* ``_coerce_api_error_detail`` — coerces a structured provider error field
  (str / dict / list / other) into a display string, preferring conventional
  message-bearing keys and recursing into nested structures.
"""
from __future__ import annotations

from types import SimpleNamespace

from run_agent import AIAgent

_STUB = SimpleNamespace()  # _clean_error_message never touches self


# ─── _clean_error_message ────────────────────────────────────────────────────


def test_empty_message_becomes_unknown_error():
    assert AIAgent._clean_error_message(_STUB, "") == "Unknown error"


def test_html_doctype_page_is_replaced_with_friendly_message():
    out = AIAgent._clean_error_message(_STUB, "<!DOCTYPE html><html><body>502</body></html>")
    assert out == "Service temporarily unavailable (HTML error page returned)"


def test_html_tag_anywhere_is_replaced():
    # Leading whitespace is stripped before the doctype check; an <html> tag
    # anywhere in the body also triggers the friendly replacement.
    assert AIAgent._clean_error_message(_STUB, "  <!DOCTYPE html ...") == (
        "Service temporarily unavailable (HTML error page returned)"
    )
    assert AIAgent._clean_error_message(_STUB, "prefix <html> suffix") == (
        "Service temporarily unavailable (HTML error page returned)"
    )


def test_html_detection_is_case_sensitive():
    # CHARACTERIZATION: the markers are matched case-sensitively, so an
    # uppercase <!DOCTYPE or <HTML> without a lowercase '<html' is NOT treated
    # as an HTML page and instead goes through whitespace/truncation cleanup.
    out = AIAgent._clean_error_message(_STUB, "<!DOCTYPE HTML> oops")
    assert out != "Service temporarily unavailable (HTML error page returned)"
    assert "oops" in out


def test_whitespace_is_collapsed():
    assert AIAgent._clean_error_message(_STUB, "a\n\n   b\t\tc") == "a b c"


def test_long_message_is_truncated_to_150_chars_with_ellipsis():
    msg = "x" * 200
    out = AIAgent._clean_error_message(_STUB, msg)
    assert out == "x" * 150 + "..."
    assert len(out) == 153


def test_exactly_150_chars_is_not_truncated():
    msg = "y" * 150
    assert AIAgent._clean_error_message(_STUB, msg) == msg


# ─── _coerce_api_error_detail ────────────────────────────────────────────────


def test_string_value_passes_through_unmodified():
    # Note: NOT stripped — the raw string is returned verbatim.
    assert AIAgent._coerce_api_error_detail("plain") == "plain"
    assert AIAgent._coerce_api_error_detail(" hi ") == " hi "


def test_dict_prefers_conventional_string_message_keys():
    assert AIAgent._coerce_api_error_detail({"message": "boom"}) == "boom"
    assert AIAgent._coerce_api_error_detail({"detail": "d"}) == "d"
    # Key priority: message beats code even when both are strings.
    assert AIAgent._coerce_api_error_detail({"code": "c", "message": "m"}) == "m"


def test_dict_recurses_into_nested_structures():
    # No top-level string field, but a nested dict under a known key.
    assert AIAgent._coerce_api_error_detail({"error": {"message": "nested"}}) == "nested"
    # A non-string scalar under a known key is coerced via the second pass.
    assert AIAgent._coerce_api_error_detail({"code": 42}) == "42"


def test_dict_without_known_keys_is_json_dumped_sorted():
    assert AIAgent._coerce_api_error_detail({"foo": "bar"}) == '{"foo": "bar"}'


def test_list_is_joined_recursively_dropping_empty_parts():
    # None coerces to "" and is filtered out of the "; "-joined result.
    assert AIAgent._coerce_api_error_detail(["a", {"message": "b"}, None]) == "a; b"


def test_none_coerces_to_empty_string():
    assert AIAgent._coerce_api_error_detail(None) == ""


def test_other_scalars_fall_back_to_str():
    assert AIAgent._coerce_api_error_detail(7) == "7"


def test_unjsonable_dict_falls_back_to_str():
    # A dict whose values can't be JSON-serialized hits the TypeError fallback.
    class _Unserializable:
        def __repr__(self):
            return "<unserializable>"

    out = AIAgent._coerce_api_error_detail({"foo": _Unserializable()})
    # json.dumps(default=...) is NOT used here → TypeError → str(dict).
    assert "unserializable" in out
