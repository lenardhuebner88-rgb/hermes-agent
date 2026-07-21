"""Edge-case characterization tests for ``agent.lsp.range_shift``.

``tests/agent/lsp/test_delta_key.py`` covers the happy paths (identity,
deletion, insertion, replacement, empty pre/post, baseline remap, pipeline).
These tests pin the boundary behavior that file does NOT exercise, so the
pure shift helpers can be refactored with full confidence:

* multi-line diagnostic straddling a deletion (end line maps to ``None`` →
  collapse to the shifted start line, keeping the diagnostic)
* diagnostic with a missing/empty ``range`` (defaults to line 0 / char 0)
* ``shift_baseline`` skipping non-dict entries
* replace-region drop at the diagnostic level
* end-of-post anchoring for pre-lines past the last opcode region
"""
from __future__ import annotations

from agent.lsp.range_shift import (
    build_line_shift,
    shift_baseline,
    shift_diagnostic_range,
)


def _diag(*, line: int, end_line: int | None = None, message: str = "m") -> dict:
    if end_line is None:
        end_line = line
    return {
        "severity": 1,
        "code": "E",
        "source": "Pyright",
        "message": message,
        "range": {
            "start": {"line": line, "character": 0},
            "end": {"line": end_line, "character": 10},
        },
    }


# ─── build_line_shift: anchoring + replace boundaries ───────────────────────


def test_shift_anchors_past_last_opcode_region_to_end_of_post():
    # Pre lines beyond the last opcode region anchor at end of post.
    shift = build_line_shift("a\nb\n", "a\nb\nc\nd\n")
    assert shift(10) == 3  # max(0, len(post)-1) == 3


def test_shift_anchors_to_none_when_post_is_empty():
    shift = build_line_shift("a\nb\n", "")
    assert shift(5) is None


# ─── shift_diagnostic_range: straddle / missing-range / replace ─────────────


def test_straddling_diagnostic_collapses_end_to_shifted_start():
    # Delete b,c (lines 1,2). A diagnostic 0→2 keeps its start (line 0) but its
    # end line 2 is deleted → collapse end to the shifted start (0), not drop.
    shift = build_line_shift("a\nb\nc\nd\n", "a\nd\n")
    remapped = shift_diagnostic_range(_diag(line=0, end_line=2), shift)
    assert remapped is not None
    assert remapped["range"]["start"]["line"] == 0
    assert remapped["range"]["end"]["line"] == 0  # collapsed to start


def test_diagnostic_with_missing_range_defaults_to_origin():
    identity = build_line_shift("a\nb\n", "a\nb\n")
    remapped = shift_diagnostic_range({"message": "no range here"}, identity)
    assert remapped is not None
    assert remapped["range"]["start"] == {"line": 0, "character": 0}
    assert remapped["range"]["end"] == {"line": 0, "character": 0}


def test_diagnostic_in_replace_region_is_dropped():
    # Replacing b with X: a diagnostic on the replaced line has no post counterpart.
    shift = build_line_shift("a\nb\nc\n", "a\nX\nc\n")
    assert shift_diagnostic_range(_diag(line=1), shift) is None


def test_shift_diagnostic_range_preserves_characters():
    shift = build_line_shift("a\nb\n", "X\na\nb\n")  # insert at top
    d = {
        "range": {
            "start": {"line": 0, "character": 3},
            "end": {"line": 0, "character": 7},
        }
    }
    remapped = shift_diagnostic_range(d, shift)
    assert remapped is not None
    assert remapped["range"]["start"] == {"line": 1, "character": 3}
    assert remapped["range"]["end"] == {"line": 1, "character": 7}


# ─── shift_baseline: skips non-dict entries ─────────────────────────────────


def test_shift_baseline_skips_non_dict_entries():
    identity = build_line_shift("a\nb\n", "a\nb\n")
    baseline = [_diag(line=0, message="keep"), "junk", 123, None, ["list"]]
    out = shift_baseline(baseline, identity)
    assert len(out) == 1
    assert out[0]["message"] == "keep"


def test_shift_baseline_empty_input_returns_empty():
    identity = build_line_shift("a\n", "a\n")
    assert shift_baseline([], identity) == []
