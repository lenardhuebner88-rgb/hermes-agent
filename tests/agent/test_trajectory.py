"""Characterization tests for the pure helpers in ``agent.trajectory``.

Covers the two side-effect-free tag helpers (``save_trajectory`` does file I/O
and is intentionally out of scope):

* ``convert_scratchpad_to_think`` — rewrites ``<REASONING_SCRATCHPAD>`` tags to
  ``<think>`` tags.
* ``has_incomplete_scratchpad`` — detects an opening tag with no closing tag.
"""

from agent.trajectory import convert_scratchpad_to_think, has_incomplete_scratchpad

OPEN = "<REASONING_SCRATCHPAD>"
CLOSE = "</REASONING_SCRATCHPAD>"

# ─── convert_scratchpad_to_think ────────────────────────────────────────────


def test_convert_returns_empty_unchanged():
    assert convert_scratchpad_to_think("") == ""


def test_convert_returns_content_without_tags_unchanged():
    text = "just some reasoning with no special tags"
    assert convert_scratchpad_to_think(text) == text


def test_convert_rewrites_complete_pair():
    assert (
        convert_scratchpad_to_think(f"{OPEN}thinking{CLOSE}")
        == "<think>thinking</think>"
    )


def test_convert_rewrites_multiple_occurrences():
    text = f"{OPEN}a{CLOSE} middle {OPEN}b{CLOSE}"
    assert convert_scratchpad_to_think(text) == "<think>a</think> middle <think>b</think>"


def test_convert_rewrites_lone_open_tag():
    # Characterization quirk: the open tag is replaced even when there is no
    # matching close tag (the two .replace() calls are independent).
    assert convert_scratchpad_to_think(f"{OPEN}streaming…") == "<think>streaming…"


def test_convert_preserves_surrounding_text():
    text = f"prefix {OPEN}inner{CLOSE} suffix"
    assert convert_scratchpad_to_think(text) == "prefix <think>inner</think> suffix"


# ─── has_incomplete_scratchpad ──────────────────────────────────────────────


def test_incomplete_false_for_empty():
    assert has_incomplete_scratchpad("") is False


def test_incomplete_false_when_no_tags():
    assert has_incomplete_scratchpad("plain text") is False


def test_incomplete_false_for_complete_pair():
    assert has_incomplete_scratchpad(f"{OPEN}done{CLOSE}") is False


def test_incomplete_true_for_open_without_close():
    assert has_incomplete_scratchpad(f"{OPEN}still going") is True


def test_incomplete_false_for_close_without_open():
    # No opening tag → not "incomplete" per the current definition.
    assert has_incomplete_scratchpad(f"orphan{CLOSE}") is False
