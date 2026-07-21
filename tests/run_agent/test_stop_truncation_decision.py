"""Characterization tests for AIAgent truncation / natural-ending detection.

* ``_has_natural_response_ending`` — heuristic: does visible assistant text look
  intentionally finished (terminal punctuation, closing fence, emoji)?
* ``_should_treat_stop_as_truncated`` — the conservative decision that an
  Ollama-hosted GLM ``finish_reason='stop'`` is actually a truncation misreport.
  Returning True triggers a continuation request; a false NEGATIVE silently
  drops a half-finished reply (work loss), a false POSITIVE fires spurious
  continuations (the #13971 regression). Both are BR 5.

The decision method is driven with a stub that binds the REAL helper methods
(``_is_ollama_glm_backend``, ``_strip_think_blocks``, ``_has_natural_response_ending``)
so the logic under test is not mocked.
"""
from __future__ import annotations

from types import SimpleNamespace

from run_agent import AIAgent


def _agent(*, model="glm-4", provider="ollama", base_url="http://localhost:11434",
           api_mode="chat_completions"):
    stub = SimpleNamespace(
        model=model,
        provider=provider,
        _base_url_lower=(base_url or "").lower(),
        api_mode=api_mode,
    )
    stub._is_ollama_glm_backend = lambda: AIAgent._is_ollama_glm_backend(stub)
    stub._strip_think_blocks = lambda c: AIAgent._strip_think_blocks(stub, c)
    stub._has_natural_response_ending = AIAgent._has_natural_response_ending
    return stub


def _msg(content, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


_TOOL_MESSAGES = [{"role": "tool", "content": "tool result"}]


# ─── _has_natural_response_ending ────────────────────────────────────────────


def test_empty_and_whitespace_only_are_not_natural_endings():
    assert AIAgent._has_natural_response_ending("") is False
    assert AIAgent._has_natural_response_ending("   \n") is False


def test_terminal_punctuation_is_a_natural_ending():
    for text in ["Done.", "Done!", "Is it?", "result:", 'quote"', "list]", "paren)"]:
        assert AIAgent._has_natural_response_ending(text) is True, text


def test_cjk_terminal_punctuation_is_a_natural_ending():
    assert AIAgent._has_natural_response_ending("他说。") is True
    assert AIAgent._has_natural_response_ending("完成！") is True


def test_closing_code_fence_is_a_natural_ending():
    assert AIAgent._has_natural_response_ending("```python\nx=1\n```") is True


def test_caret_is_a_natural_ending():
    assert AIAgent._has_natural_response_ending("see above^") is True


def test_emoji_is_a_natural_ending():
    assert AIAgent._has_natural_response_ending("all done 🎉") is True


def test_text_ending_in_letter_or_comma_is_not_natural():
    assert AIAgent._has_natural_response_ending("the answer is") is False
    assert AIAgent._has_natural_response_ending("wait,") is False


# ─── _should_treat_stop_as_truncated ─────────────────────────────────────────

# A long, whitespace-bearing reply that does NOT end naturally → truncated.
_UNFINISHED = "The answer to your question is 42 and"


def test_unfinished_ollama_glm_stop_after_tool_use_is_truncated():
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(_UNFINISHED), _TOOL_MESSAGES
    ) is True


def test_length_finish_reason_is_not_reclassified():
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "length", _msg(_UNFINISHED), _TOOL_MESSAGES
    ) is False


def test_non_chat_completions_mode_is_not_reclassified():
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(api_mode="anthropic_messages"), "stop", _msg(_UNFINISHED), _TOOL_MESSAGES
    ) is False


def test_non_ollama_glm_backend_is_not_reclassified():
    # qwen on ollama reports stop correctly → leave it alone (#13971 guard).
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(model="qwen3:32b"), "stop", _msg(_UNFINISHED), _TOOL_MESSAGES
    ) is False


def test_requires_a_prior_tool_message():
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(_UNFINISHED), [{"role": "user", "content": "hi"}]
    ) is False
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(_UNFINISHED), None
    ) is False


def test_none_message_or_pending_tool_calls_are_not_truncated():
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", None, _TOOL_MESSAGES
    ) is False
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(_UNFINISHED, tool_calls=[{"id": "c1"}]), _TOOL_MESSAGES
    ) is False


def test_non_string_content_is_not_truncated():
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(["block"]), _TOOL_MESSAGES
    ) is False


def test_empty_or_short_or_whitespaceless_content_is_not_truncated():
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(""), _TOOL_MESSAGES
    ) is False
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg("too short"), _TOOL_MESSAGES
    ) is False  # < 20 chars
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg("onelongtokenwithoutanyspaceshere"), _TOOL_MESSAGES
    ) is False  # no whitespace


def test_naturally_ending_content_is_not_truncated():
    finished = "The computation completed successfully."
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(finished), _TOOL_MESSAGES
    ) is False


def test_think_blocks_are_stripped_before_the_length_check():
    # Visible text after stripping the think block is short → NOT truncated,
    # proving the decision runs on visible text, not the raw content length.
    content = "<think>" + "x" * 500 + "</think>short bit here"
    assert AIAgent._should_treat_stop_as_truncated(
        _agent(), "stop", _msg(content), _TOOL_MESSAGES
    ) is False
