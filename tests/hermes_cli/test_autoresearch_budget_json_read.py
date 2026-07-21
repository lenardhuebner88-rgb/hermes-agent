"""Characterization tests for autoresearch_budget JSON state readers.

A deliberately OPPOSITE pair — confusing them is a safety bug:

* ``_read_json`` — TOLERANT (fail-open) for OPTIONAL cooldown state. Garbage
  degrades to ``{}``; it must NEVER raise, so a corrupt cooldown file can't
  crash the loop.
* ``_read_json_fail_closed`` — STRICT (fail-closed) for the BUDGET ledger. A
  missing/empty file is a fresh day-zero state, but an unreadable/unparsable/
  wrong-typed file must raise ``BudgetExhausted`` — silently returning ``{}``
  there would reset the budget and re-open ~30 calls / 100k tokens (money).

TEST-ONLY: budget code is never refactored (per the brief's forbidden list).
"""
from __future__ import annotations

import json

import pytest

from hermes_cli.autoresearch_budget import (
    BudgetExhausted,
    _read_json,
    _read_json_fail_closed,
)

# ─── _read_json: tolerant, never raises ──────────────────────────────────────


def test_tolerant_missing_file_is_empty_dict(tmp_path):
    assert _read_json(tmp_path / "absent.json") == {}


def test_tolerant_empty_file_is_empty_dict(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("", encoding="utf-8")
    assert _read_json(p) == {}


def test_tolerant_valid_dict_is_returned(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"cooldown_until": 123}), encoding="utf-8")
    assert _read_json(p) == {"cooldown_until": 123}


def test_tolerant_non_dict_json_is_empty_dict(tmp_path):
    for payload in ("[1, 2]", '"just a string"', "42"):
        p = tmp_path / "state.json"
        p.write_text(payload, encoding="utf-8")
        assert _read_json(p) == {}, payload


def test_tolerant_corrupt_json_is_empty_dict_and_never_raises(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert _read_json(p) == {}  # must not raise


def test_tolerant_whitespace_only_file_is_empty_dict(tmp_path):
    # Non-zero size, so it bypasses the ``st_size == 0`` short-circuit and hits
    # ``json.loads`` -> ValueError. Pins that a half-written/blanked cooldown
    # file degrades to {} rather than raising.
    p = tmp_path / "state.json"
    p.write_text("   \n\t  ", encoding="utf-8")
    assert _read_json(p) == {}


def test_tolerant_binary_non_utf8_is_empty_dict(tmp_path):
    # Raw bytes that are not valid UTF-8: ``read_text(encoding="utf-8")`` raises
    # UnicodeDecodeError (a ValueError subclass). Pins the decode-error path —
    # narrowing the except to json.JSONDecodeError would let this leak.
    p = tmp_path / "state.json"
    p.write_bytes(b"\x80\x81\x82\xff\xfe")
    assert _read_json(p) == {}


def test_tolerant_permission_error_is_empty_dict(tmp_path, monkeypatch):
    # Pins the OSError arm (unreadable file), which no other test exercises.
    # The file exists and is non-empty (passes the exists()/st_size guards) but
    # the read itself is denied -> PermissionError (an OSError) -> fail-open {}.
    import pathlib

    p = tmp_path / "state.json"
    p.write_text('{"cooldown_until": 1}', encoding="utf-8")

    def _denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pathlib.Path, "read_text", _denied)
    assert _read_json(p) == {}  # must not raise


# ─── _read_json_fail_closed: strict, raises BudgetExhausted on corruption ────


def test_fail_closed_missing_file_is_fresh_state(tmp_path):
    assert _read_json_fail_closed(tmp_path / "absent.json") == {}


def test_fail_closed_empty_or_whitespace_is_fresh_state(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("", encoding="utf-8")
    assert _read_json_fail_closed(p) == {}
    p.write_text("   \n", encoding="utf-8")
    assert _read_json_fail_closed(p) == {}


def test_fail_closed_valid_dict_is_returned(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"calls": 5, "tokens": 1000}), encoding="utf-8")
    assert _read_json_fail_closed(p) == {"calls": 5, "tokens": 1000}


def test_fail_closed_corrupt_json_raises_budget_exhausted(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(BudgetExhausted, match="corrupt"):
        _read_json_fail_closed(p)


def test_fail_closed_non_dict_top_level_raises_budget_exhausted(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(BudgetExhausted, match="not an object"):
        _read_json_fail_closed(p)


# ─── the safety contrast ─────────────────────────────────────────────────────


def test_same_corrupt_input_fail_open_vs_fail_closed(tmp_path):
    # The exact same corrupt file: cooldown reader shrugs it off, the budget
    # ledger reader refuses to continue. This asymmetry IS the safety property.
    p = tmp_path / "shared.json"
    p.write_text("garbage-not-json", encoding="utf-8")
    assert _read_json(p) == {}
    with pytest.raises(BudgetExhausted):
        _read_json_fail_closed(p)
