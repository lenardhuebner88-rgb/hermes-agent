"""Coverage for ``kanban_db._claude_profile_instructions``.

The claude-CLI / Max-subscription worker path does not enter
``hermes -p <profile> chat``, so SOUL.md is injected into the worker prompt to
keep profile hardening active. This helper must be fail-soft (a missing or empty
SOUL.md must never block a spawn) and must bound the injected size.
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb


def test_returns_empty_without_home():
    assert kb._claude_profile_instructions(None) == ""


def test_reads_soul_md(tmp_path):
    (tmp_path / "SOUL.md").write_text("PROFILE DISCIPLINE", encoding="utf-8")
    assert kb._claude_profile_instructions(str(tmp_path)) == "PROFILE DISCIPLINE"


def test_empty_when_soul_missing(tmp_path):
    assert kb._claude_profile_instructions(str(tmp_path)) == ""


def test_empty_when_soul_blank(tmp_path):
    (tmp_path / "SOUL.md").write_text("   \n  ", encoding="utf-8")
    assert kb._claude_profile_instructions(str(tmp_path)) == ""


def test_truncates_oversized_soul(tmp_path):
    (tmp_path / "SOUL.md").write_text("x" * 20000, encoding="utf-8")
    out = kb._claude_profile_instructions(str(tmp_path), max_chars=100)
    assert out.endswith("[SOUL.md truncated by dispatcher]")
    assert len(out) < 200
