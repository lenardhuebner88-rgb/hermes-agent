"""Characterization tests for pure helpers in the root ``utils`` module.

Covers five leaf functions that previously had no dedicated tests:

* ``safe_json_loads`` — parse-or-default JSON
* ``env_int`` / ``env_bool`` — typed environment reads
* ``normalize_proxy_url`` / ``normalize_proxy_env_vars`` — SOCKS alias canonicalization

These pin current observable behavior (including a couple of quirks that a
future refactor must preserve) so the helpers can be safely refactored under
the protection of this suite.
"""

from utils import (
    env_bool,
    env_float,
    env_int,
    normalize_proxy_env_vars,
    normalize_proxy_url,
    safe_json_loads,
)

# ─── safe_json_loads ────────────────────────────────────────────────────────


def test_safe_json_loads_parses_objects_and_arrays():
    assert safe_json_loads('{"a": 1}') == {"a": 1}
    assert safe_json_loads("[1, 2, 3]") == [1, 2, 3]


def test_safe_json_loads_parses_scalar_json():
    assert safe_json_loads('"hi"') == "hi"
    assert safe_json_loads("42") == 42
    assert safe_json_loads("3.5") == 3.5
    assert safe_json_loads("true") is True


def test_safe_json_loads_returns_falsy_values_not_default():
    # A valid-but-falsy parse result must be returned verbatim, never confused
    # with the default. (Guards against a `return parsed or default` bug.)
    sentinel = object()
    assert safe_json_loads("0", default=sentinel) == 0
    assert safe_json_loads('""', default=sentinel) == ""
    assert safe_json_loads("[]", default=sentinel) == []
    assert safe_json_loads("false", default=sentinel) is False
    assert safe_json_loads("null", default=sentinel) is None


def test_safe_json_loads_returns_default_on_invalid_json():
    assert safe_json_loads("{not json") is None
    assert safe_json_loads("{'single': 'quotes'}", default="fb") == "fb"
    assert safe_json_loads("", default=[]) == []


def test_safe_json_loads_returns_default_on_non_parseable_input():
    # None / wrong types raise TypeError inside json.loads → default.
    assert safe_json_loads(None, default="fb") == "fb"  # type: ignore[arg-type]
    assert safe_json_loads(12345, default="fb") == "fb"  # type: ignore[arg-type]


# ─── env_int ────────────────────────────────────────────────────────────────


def test_env_int_reads_valid_integer(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_INT", "42")
    assert env_int("HERMES_TEST_INT") == 42


def test_env_int_strips_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_INT", "  10  ")
    assert env_int("HERMES_TEST_INT") == 10


def test_env_int_handles_negative(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_INT", "-7")
    assert env_int("HERMES_TEST_INT") == -7


def test_env_int_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_INT", raising=False)
    assert env_int("HERMES_TEST_INT") == 0
    assert env_int("HERMES_TEST_INT", default=99) == 99


def test_env_int_returns_default_when_blank(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_INT", "   ")
    assert env_int("HERMES_TEST_INT", default=5) == 5


def test_env_int_returns_default_when_not_an_int(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_INT", "abc")
    assert env_int("HERMES_TEST_INT", default=3) == 3
    # A float string is not a valid int() literal → default.
    monkeypatch.setenv("HERMES_TEST_INT", "3.5")
    assert env_int("HERMES_TEST_INT", default=3) == 3


# ─── env_float ──────────────────────────────────────────────────────────────


def test_env_float_reads_valid_float(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_FLOAT", "3.5")
    assert env_float("HERMES_TEST_FLOAT") == 3.5


def test_env_float_strips_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_FLOAT", "  2.5  ")
    assert env_float("HERMES_TEST_FLOAT") == 2.5


def test_env_float_handles_negative_and_scientific(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_FLOAT", "-1.5")
    assert env_float("HERMES_TEST_FLOAT") == -1.5
    monkeypatch.setenv("HERMES_TEST_FLOAT", "1e3")
    assert env_float("HERMES_TEST_FLOAT") == 1000.0


def test_env_float_accepts_integer_string(monkeypatch):
    # Unlike env_int, a bare integer string is a valid float literal.
    monkeypatch.setenv("HERMES_TEST_FLOAT", "42")
    assert env_float("HERMES_TEST_FLOAT") == 42.0


def test_env_float_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_FLOAT", raising=False)
    assert env_float("HERMES_TEST_FLOAT") == 0.0
    assert env_float("HERMES_TEST_FLOAT", default=9.9) == 9.9


def test_env_float_returns_default_when_blank_or_invalid(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_FLOAT", "   ")
    assert env_float("HERMES_TEST_FLOAT", default=1.5) == 1.5
    monkeypatch.setenv("HERMES_TEST_FLOAT", "abc")
    assert env_float("HERMES_TEST_FLOAT", default=1.5) == 1.5


# ─── env_bool ───────────────────────────────────────────────────────────────


def test_env_bool_truthy_strings(monkeypatch):
    for val in ("1", "true", "YES", " on ", "True"):
        monkeypatch.setenv("HERMES_TEST_BOOL", val)
        assert env_bool("HERMES_TEST_BOOL") is True, val


def test_env_bool_falsey_strings(monkeypatch):
    for val in ("0", "false", "off", "no", ""):
        monkeypatch.setenv("HERMES_TEST_BOOL", val)
        assert env_bool("HERMES_TEST_BOOL") is False, val


def test_env_bool_unset_is_false_even_with_default_true(monkeypatch):
    # Characterization quirk: env_bool reads ``os.getenv(key, "")`` so an unset
    # var becomes the empty *string* (not None) and ``is_truthy_value``'s
    # ``default`` branch is never reached — the result is False regardless of
    # the passed default. A refactor must preserve this exact behavior.
    monkeypatch.delenv("HERMES_TEST_BOOL", raising=False)
    assert env_bool("HERMES_TEST_BOOL", default=True) is False
    assert env_bool("HERMES_TEST_BOOL", default=False) is False


# ─── normalize_proxy_url ────────────────────────────────────────────────────


def test_normalize_proxy_url_none_and_blank_return_none():
    assert normalize_proxy_url(None) is None
    assert normalize_proxy_url("") is None
    assert normalize_proxy_url("   ") is None


def test_normalize_proxy_url_rewrites_socks_alias_to_socks5():
    assert normalize_proxy_url("socks://127.0.0.1:1080") == "socks5://127.0.0.1:1080"


def test_normalize_proxy_url_socks_rewrite_is_case_insensitive():
    # Only the scheme prefix is matched case-insensitively; the remainder is
    # carried through verbatim.
    assert normalize_proxy_url("SOCKS://Host.Example:1080") == "socks5://Host.Example:1080"


def test_normalize_proxy_url_strips_surrounding_whitespace():
    assert normalize_proxy_url("  socks://h:1  ") == "socks5://h:1"


def test_normalize_proxy_url_leaves_other_schemes_unchanged():
    assert normalize_proxy_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert normalize_proxy_url("http://proxy:8080") == "http://proxy:8080"
    assert normalize_proxy_url("https://proxy:443") == "https://proxy:443"


# ─── normalize_proxy_env_vars ───────────────────────────────────────────────


def test_normalize_proxy_env_vars_rewrites_socks_in_place(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "socks://h:1")
    monkeypatch.setenv("http_proxy", "socks://h:2")
    normalize_proxy_env_vars()
    import os

    assert os.environ["HTTPS_PROXY"] == "socks5://h:1"
    assert os.environ["http_proxy"] == "socks5://h:2"


def test_normalize_proxy_env_vars_leaves_canonical_values(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy:8080")
    normalize_proxy_env_vars()
    import os

    assert os.environ["HTTP_PROXY"] == "http://proxy:8080"


def test_normalize_proxy_env_vars_does_not_create_blank_vars(monkeypatch):
    # Unset/blank proxy vars normalize to None and must not be written back.
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    normalize_proxy_env_vars()
    import os

    assert "ALL_PROXY" not in os.environ
    assert "all_proxy" not in os.environ
