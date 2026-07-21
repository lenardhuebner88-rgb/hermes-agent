"""Characterization tests for tool_executor resilience helpers.

* ``_flush_session_db_after_tool_progress`` — best-effort incremental SessionDB
  flush after a tool call. It MUST NEVER raise: tool side effects can kill the
  process before normal turn-end persistence, so this flush is the transcript's
  only lifeline — but if the flush itself throws, it must be swallowed (logged),
  never allowed to abort the tool batch. A regression that lets it propagate
  would turn a harmless persistence hiccup into a crashed agent loop.
* ``_is_interpreter_shutdown_submit_error`` — classifies the RuntimeError that
  means "the interpreter is shutting down, don't retry submitting work", so the
  executor doesn't spin retrying a dead pool during shutdown.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from agent.tool_executor import (
    _flush_session_db_after_tool_progress,
    _is_interpreter_shutdown_submit_error,
)

# ─── _flush_session_db_after_tool_progress ───────────────────────────────────


def test_flush_delegates_to_the_agent_with_messages():
    calls = {}

    def _flush(messages):
        calls["messages"] = messages

    agent = SimpleNamespace(_flush_messages_to_session_db=_flush)
    msgs = [{"role": "assistant"}, {"role": "tool"}]
    _flush_session_db_after_tool_progress(agent, msgs, stage="mid_tool")
    assert calls["messages"] is msgs


def test_flush_swallows_exceptions_from_the_agent(caplog):
    def _boom(messages):
        raise RuntimeError("session db locked")

    agent = SimpleNamespace(_flush_messages_to_session_db=_boom)
    # Must not raise.
    with caplog.at_level(logging.WARNING):
        _flush_session_db_after_tool_progress(agent, [{"role": "tool"}], stage="mid_tool")
    assert any("persistence failed" in r.message for r in caplog.records)


def test_flush_swallows_broad_exceptions(caplog):
    def _boom(messages):
        raise ValueError("disk full")

    agent = SimpleNamespace(_flush_messages_to_session_db=_boom)
    _flush_session_db_after_tool_progress(agent, [], stage="end_tool")  # no raise


def test_flush_swallows_missing_method(caplog):
    # An agent that lacks the flush method entirely (AttributeError) is still
    # tolerated — best-effort means best-effort.
    agent = SimpleNamespace()
    _flush_session_db_after_tool_progress(agent, [], stage="mid_tool")  # no raise


# ─── _is_interpreter_shutdown_submit_error ───────────────────────────────────


def test_shutdown_submit_error_is_detected():
    exc = RuntimeError("cannot schedule new futures after interpreter shutdown")
    assert _is_interpreter_shutdown_submit_error(exc) is True


def test_marker_anywhere_in_message_is_detected():
    exc = RuntimeError("wrap: cannot schedule new futures after interpreter shutdown (x)")
    assert _is_interpreter_shutdown_submit_error(exc) is True


def test_other_runtime_errors_are_not_shutdown_errors():
    assert _is_interpreter_shutdown_submit_error(RuntimeError("pool is closed")) is False
    assert _is_interpreter_shutdown_submit_error(RuntimeError("")) is False
