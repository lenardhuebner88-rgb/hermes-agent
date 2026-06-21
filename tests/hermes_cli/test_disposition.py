"""Tests for ``hermes_cli.disposition`` — Disposition schema + validator/parser.

Metadata key convention: ``metadata["disposition"]["items"]`` (list).

Coverage:
  - Happy-path parse with all valid fields.
  - Each validate_disposition business rule.
  - Empty items list is valid (no follow-ups / risks = ok).
  - Entirely missing disposition field fails hard.
  - Alt-metadata (no disposition key, None, non-dict) passes parse without exception.
  - Invalid enum values → validation fails with item-index in missing.
  - next_action required when disposition is "delegate" or "defer".
  - LLM-refusal / truncation markers treated as invalid (not empty-valid).
"""

from __future__ import annotations

import pytest

from hermes_cli import disposition as d


# ---------------------------------------------------------------------------
# parse_disposition — tolerant path
# ---------------------------------------------------------------------------


def test_parse_happy_path_returns_items():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "risk",
                    "disposition": "done",
                    "next_action": "verify test coverage",
                    "severity": "real-risk",
                    "evidence": "tests/test_foo.py:42",
                },
                {
                    "typ": "follow_up",
                    "disposition": "defer",
                    "next_action": "revisit next sprint",
                    "severity": "none",
                    "evidence": "task_id:t_abc123",
                },
            ]
        }
    }
    result = d.parse_disposition(metadata)
    assert len(result.items) == 2
    assert result.items[0].typ == "risk"
    assert result.items[0].disposition == "done"
    assert result.items[0].severity == "real-risk"
    assert result.items[1].typ == "follow_up"
    assert result.items[1].next_action == "revisit next sprint"


def test_parse_metadata_none_returns_empty():
    result = d.parse_disposition(None)
    assert result.items == []


def test_parse_metadata_not_dict_returns_empty():
    result = d.parse_disposition("some string")
    assert result.items == []


def test_parse_metadata_no_disposition_key_returns_empty():
    # backward-compat: old done-tasks carry no "disposition" key
    result = d.parse_disposition({"artifacts": ["foo.py"], "summary": "done"})
    assert result.items == []


def test_parse_disposition_key_not_dict_returns_empty():
    result = d.parse_disposition({"disposition": "invalid_scalar"})
    assert result.items == []


def test_parse_items_not_list_returns_empty():
    result = d.parse_disposition({"disposition": {"items": "oops"}})
    assert result.items == []


def test_parse_invalid_item_skipped_others_kept():
    metadata = {
        "disposition": {
            "items": [
                {"typ": "INVALID_TYP", "disposition": "done", "next_action": "n/a", "severity": "none", "evidence": "x"},
                {"typ": "risk", "disposition": "drop", "next_action": "nothing", "severity": "real-risk", "evidence": "x"},
            ]
        }
    }
    result = d.parse_disposition(metadata)
    # Invalid items are skipped — only the valid one remains
    assert len(result.items) == 1
    assert result.items[0].typ == "risk"


def test_parse_extra_keys_ignored():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "still_open",
                    "disposition": "delegate",
                    "next_action": "hand off to ops",
                    "severity": "none",
                    "evidence": "commit:abc",
                    "unknown_future_key": "ignored",
                }
            ],
            "schema_version": 2,
        }
    }
    result = d.parse_disposition(metadata)
    assert len(result.items) == 1
    assert result.items[0].typ == "still_open"


def test_parse_severity_defaults_to_none_for_non_risk():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "follow_up",
                    "disposition": "done",
                    "next_action": "check docs",
                    "evidence": "file:1",
                    # severity intentionally omitted
                }
            ]
        }
    }
    result = d.parse_disposition(metadata)
    assert result.items[0].severity == "none"


# ---------------------------------------------------------------------------
# validate_disposition — strict path
# ---------------------------------------------------------------------------


def test_validate_empty_items_is_valid():
    """Explicit empty list = 'no follow-ups / risks' — a legitimate outcome."""
    ok, missing = d.validate_disposition({"disposition": {"items": []}})
    assert ok is True
    assert missing == []


def test_validate_missing_disposition_key_fails():
    ok, missing = d.validate_disposition({"artifacts": ["x.py"]})
    assert ok is False
    assert "disposition" in missing


def test_validate_metadata_none_fails():
    ok, missing = d.validate_disposition(None)
    assert ok is False
    assert "disposition" in missing


def test_validate_metadata_not_dict_fails():
    ok, missing = d.validate_disposition(42)
    assert ok is False
    assert "disposition" in missing


def test_validate_items_not_list_fails():
    ok, missing = d.validate_disposition({"disposition": {"items": "oops"}})
    assert ok is False
    assert any("items" in m for m in missing)


def test_validate_happy_path_single_item():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "risk",
                    "disposition": "done",
                    "next_action": "monitor",
                    "severity": "real-risk",
                    "evidence": "commit:abc123",
                }
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is True
    assert missing == []


def test_validate_invalid_typ_fails():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "UNKNOWN",
                    "disposition": "done",
                    "next_action": "n/a",
                    "severity": "none",
                    "evidence": "x",
                }
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    assert any("typ" in m for m in missing)


def test_validate_invalid_disposition_enum_fails():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "follow_up",
                    "disposition": "INVALID",
                    "next_action": "n/a",
                    "severity": "none",
                    "evidence": "x",
                }
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    assert any("disposition" in m for m in missing)


def test_validate_next_action_required_for_delegate():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "follow_up",
                    "disposition": "delegate",
                    # next_action intentionally missing
                    "severity": "none",
                    "evidence": "x",
                }
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    assert any("next_action" in m for m in missing)


def test_validate_next_action_required_for_defer():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "still_open",
                    "disposition": "defer",
                    # next_action missing
                    "severity": "none",
                    "evidence": "x",
                }
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    assert any("next_action" in m for m in missing)


def test_validate_next_action_not_required_for_done():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "risk",
                    "disposition": "done",
                    # next_action omitted — allowed for done/drop
                    "severity": "real-risk",
                    "evidence": "x",
                }
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is True
    assert missing == []


def test_validate_next_action_not_required_for_drop():
    metadata = {
        "disposition": {
            "items": [
                {
                    "typ": "follow_up",
                    "disposition": "drop",
                    "severity": "none",
                    "evidence": "x",
                }
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is True
    assert missing == []


def test_validate_missing_reports_item_index():
    metadata = {
        "disposition": {
            "items": [
                # item 0: valid
                {
                    "typ": "risk",
                    "disposition": "done",
                    "next_action": "ok",
                    "severity": "real-risk",
                    "evidence": "x",
                },
                # item 1: invalid typ
                {
                    "typ": "BAD",
                    "disposition": "done",
                    "next_action": "ok",
                    "severity": "none",
                    "evidence": "x",
                },
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    # The index 1 must appear in at least one missing entry
    assert any("[1]" in m for m in missing)


def test_validate_multiple_items_multiple_errors():
    metadata = {
        "disposition": {
            "items": [
                {"typ": "BAD", "disposition": "done", "next_action": "ok", "severity": "none", "evidence": "x"},
                {"typ": "follow_up", "disposition": "defer", "severity": "none", "evidence": "x"},
            ]
        }
    }
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    # Must report both items' issues
    assert len(missing) >= 2


# ---------------------------------------------------------------------------
# LLM-refusal / truncation markers
# ---------------------------------------------------------------------------


def test_parse_llm_refusal_marker_skips_gracefully():
    """A metadata dict with a refusal marker must not crash parse."""
    metadata = {"disposition": {"__llm_refusal__": True, "items": []}}
    # parse is tolerant — empty or partial result is fine, no exception
    result = d.parse_disposition(metadata)
    assert isinstance(result.items, list)


def test_validate_llm_refusal_marker_is_invalid():
    """A refusal marker makes the disposition field semantically invalid."""
    metadata = {"disposition": {"__llm_refusal__": True, "items": []}}
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    assert any("refusal" in m.lower() or "llm" in m.lower() for m in missing)


def test_validate_truncation_marker_is_invalid():
    """A truncation marker makes the disposition field semantically invalid."""
    metadata = {"disposition": {"__truncated__": True, "items": [{"typ": "risk", "disposition": "done"}]}}
    ok, missing = d.validate_disposition(metadata)
    assert ok is False
    assert any("truncat" in m.lower() for m in missing)


def test_parse_truncation_marker_does_not_crash():
    metadata = {"disposition": {"__truncated__": True, "items": []}}
    result = d.parse_disposition(metadata)
    assert isinstance(result.items, list)


# ---------------------------------------------------------------------------
# severity — CHARACTERIZATION guards (pin the deliberately-soft Phase-0 behavior)
#
# severity is intentionally NOT enforced yet (Phase 4 will tighten it). These two
# tests pin the current soft behavior so that the Phase-4 strictness change shows up
# as a RED test here instead of slipping through as a silent false-green — the exact
# failure mode that bit the review-tier classify boilerplate bug (dogfood 2026-06-21).
# When Phase 4 lands, update these alongside the new strict-validation tests.
# ---------------------------------------------------------------------------

def test_validate_accepts_invalid_severity_currently():
    """CHARACTERIZATION: validate_disposition does NOT yet check the severity enum —
    an unknown severity passes as long as typ/disposition are valid."""
    metadata = {"disposition": {"items": [
        {"typ": "risk", "disposition": "done", "severity": "BOGUS", "evidence": "x"}]}}
    ok, missing = d.validate_disposition(metadata)
    assert ok is True
    assert not any("severity" in m for m in missing)


def test_parse_coerces_unknown_severity_to_none_currently():
    """CHARACTERIZATION: parse_disposition silently coerces an *unknown* severity
    VALUE to 'none' (not only an absent one). Pins the coercion path."""
    result = d.parse_disposition({"disposition": {"items": [
        {"typ": "risk", "disposition": "done", "severity": "BOGUS", "evidence": "x"}]}})
    assert len(result.items) == 1
    assert result.items[0].severity == "none"
