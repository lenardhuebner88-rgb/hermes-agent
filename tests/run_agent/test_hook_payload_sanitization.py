"""Characterization tests for AIAgent plugin-hook payload sanitization.

``_hook_jsonable`` / ``_sanitize_hook_payload`` turn arbitrary Python objects
(SDK response/message objects, dataclasses, pydantic models, raw dicts) into
JSON-safe payloads for observability hooks. A regression here either crashes
every hook invocation (observability goes dark — call sites swallow the
exception), stack-overflows on a circular reference, or leaks a sensitive key.

Existing coverage (test_run_agent.py::TestHookPayloadSanitizesSimpleNamespace)
only pins SimpleNamespace normalization. This file pins the rest: the depth
limit (the circular-ref guard), string/sequence truncation, sensitive-key
redaction, pydantic/dataclass/__dict__ introspection, and the multi-pass
shrink + ``_truncated`` fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from run_agent import AIAgent

# ─── scalars & primitives ────────────────────────────────────────────────────


def test_primitives_pass_through():
    assert AIAgent._hook_jsonable(None) is None
    assert AIAgent._hook_jsonable(True) is True
    assert AIAgent._hook_jsonable(42) == 42
    assert AIAgent._hook_jsonable(3.5) == 3.5
    assert AIAgent._hook_jsonable("hi") == "hi"


def test_long_string_is_truncated_with_count_suffix():
    s = "x" * 8010
    out = AIAgent._hook_jsonable(s, max_string=8000)
    assert out.startswith("x" * 8000)
    assert "truncated 10 chars" in out


def test_bytes_are_summarized_not_dumped():
    assert AIAgent._hook_jsonable(b"\x00\x01\x02") == "<3 bytes>"
    assert AIAgent._hook_jsonable(bytearray(b"ab")) == "<2 bytes>"


# ─── depth limit = circular-reference guard ──────────────────────────────────


def test_depth_limit_bottoms_out_instead_of_recurring_forever():
    # A self-referential dict must NOT raise RecursionError; the depth limit
    # replaces the too-deep node with a marker string.
    d: dict = {"k": "v"}
    d["self"] = d
    out = AIAgent._hook_jsonable(d)  # must not raise
    # Walk down the "self" chain until we hit the depth-limit marker.
    node = out
    seen_depth = 0
    while isinstance(node, dict) and "self" in node:
        node = node["self"]
        seen_depth += 1
        assert seen_depth < 50  # bounded, not infinite
    assert isinstance(node, str) and "depth limit" in node


def test_explicit_depth_over_max_returns_marker():
    assert AIAgent._hook_jsonable({"a": 1}, depth=9, max_depth=8) == "<dict depth limit>"


# ─── dict handling: key stringification + sensitive redaction ────────────────


def test_dict_keys_are_stringified_and_values_recursed():
    out = AIAgent._hook_jsonable({1: "a", "b": {"c": 2}})
    assert out == {"1": "a", "b": {"c": 2}}


def test_sensitive_keys_are_redacted():
    out = AIAgent._hook_jsonable(
        {
            "api_key": "sk-secret",
            "Authorization": "Bearer xyz",
            "X_API_KEY": "also-secret",  # normalized x_api_key endswith _api_key
            "normal": "visible",
        }
    )
    assert out["api_key"] == "<redacted>"
    assert out["Authorization"] == "<redacted>"
    assert out["X_API_KEY"] == "<redacted>"
    assert out["normal"] == "visible"


def test_dict_over_sequence_cap_records_truncation():
    big = {str(i): i for i in range(10)}
    out = AIAgent._hook_jsonable(big, max_sequence=3)
    assert out["_truncated_items"] == 7  # 10 - 3
    # Only the first 3 real keys plus the truncation marker.
    assert len([k for k in out if k != "_truncated_items"]) == 3


# ─── sequence handling ───────────────────────────────────────────────────────


def test_list_and_tuple_are_recursed():
    assert AIAgent._hook_jsonable([1, {"a": 2}]) == [1, {"a": 2}]
    assert AIAgent._hook_jsonable((1, 2)) == [1, 2]


def test_single_element_set_becomes_list():
    assert AIAgent._hook_jsonable({7}) == [7]


def test_sequence_over_cap_appends_truncation_marker():
    out = AIAgent._hook_jsonable(list(range(10)), max_sequence=4)
    assert out[:4] == [0, 1, 2, 3]
    assert out[-1] == {"_truncated_items": 6}  # 10 - 4


# ─── object introspection: pydantic / dataclass / __dict__ ───────────────────


def test_model_dump_object_is_normalized():
    class _FakePydantic:
        def model_dump(self, mode=None):
            return {"field": "value", "n": 1}

    assert AIAgent._hook_jsonable(_FakePydantic()) == {"field": "value", "n": 1}


def test_dataclass_is_normalized_via_asdict():
    @dataclass
    class _Point:
        x: int
        y: int

    assert AIAgent._hook_jsonable(_Point(1, 2)) == {"x": 1, "y": 2}


def test_plain_object_uses_public_attrs_only():
    class _Obj:
        def __init__(self):
            self.public = "shown"
            self._private = "hidden"

    out = AIAgent._hook_jsonable(_Obj())
    assert out == {"public": "shown"}  # underscore-prefixed attrs excluded


def test_opaque_object_falls_back_to_str():
    # A slots class with no __dict__ skips the public-attrs branch and falls
    # through to str(value).
    class _Slots:
        __slots__ = ()

        def __str__(self):
            return "slots-repr"

    assert AIAgent._hook_jsonable(_Slots()) == "slots-repr"


# ─── _sanitize_hook_payload: multi-pass shrink + _truncated fallback ─────────


def test_small_payload_is_returned_normalized():
    out = AIAgent._sanitize_hook_payload({"a": 1, "b": "two"})
    assert out == {"a": 1, "b": "two"}


def test_oversize_payload_falls_back_to_truncated_envelope(monkeypatch):
    monkeypatch.setenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "1000")  # floor
    payload = {"blob": "z" * 5000}
    out = AIAgent._sanitize_hook_payload(payload)
    assert out["_truncated"] is True
    assert out["original_type"] == "dict"
    assert len(out["preview"]) <= 1000


def test_payload_max_chars_floor_and_invalid_env():
    # The limit never drops below 1000 and invalid env falls back to 50000.
    import os

    os.environ["HERMES_PLUGIN_PAYLOAD_MAX_CHARS"] = "5"
    try:
        assert AIAgent._hook_payload_max_chars() == 1000  # floored
    finally:
        del os.environ["HERMES_PLUGIN_PAYLOAD_MAX_CHARS"]
    os.environ["HERMES_PLUGIN_PAYLOAD_MAX_CHARS"] = "garbage"
    try:
        assert AIAgent._hook_payload_max_chars() == 50000  # default on ValueError
    finally:
        del os.environ["HERMES_PLUGIN_PAYLOAD_MAX_CHARS"]
