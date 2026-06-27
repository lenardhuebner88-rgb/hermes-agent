"""Integration coverage for the versioned completion-bundle gate (PlanSpec C).

Two guarantees land here, end-to-end:

  1. Reject-recovery: a worker that opts into the structured bundle (metadata
     carries ``schema_version``) but submits it INCOMPLETE is rejected IN-FLIGHT
     by the kanban_complete entry points — the in-process tool (`_handle_complete`)
     and the claude-CLI verb (`_cmd_complete`) — with the missing fields echoed
     back and NO state change. This replaces the old "land half-filled bundle →
     downstream gate auto-blocks" loop with a retry-in-place.

  2. Render guarantee: the mandatory bundle fields survive the 4 KB per-field cap
     in ``build_worker_context`` even when a large disposition/changed_files array
     would otherwise push them off the tail — so the reviewer always sees them.

Backward-compat: a legacy completion with no ``schema_version`` is never gated.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with a claimed (running) task; HERMES_KANBAN_TASK set."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="bundle-gate-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _status(tid: str) -> str:
    conn = kb.connect()
    try:
        return kb.get_task(conn, tid).status
    finally:
        conn.close()


_COMPLETE = {
    "schema_version": 1,
    "gates": {"exit_code": 0, "command": "scripts/run-affected.sh"},
    "AC": "AC-1/AC-2/AC-3 covered",
    "residual_risk": "none",
}


# ---------------------------------------------------------------------------
# 1. In-process tool path (_handle_complete)
# ---------------------------------------------------------------------------


def test_tool_rejects_incomplete_bundle_in_flight(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_complete({"summary": "done", "metadata": {"schema_version": 1}})
    d = json.loads(out)
    assert d.get("ok") is not True
    err = d.get("error", "")
    assert "missing required field" in err
    assert "gates.exit_code" in err and "residual_risk" in err
    # IN-FLIGHT: the task did not change state.
    assert _status(worker_env) == "running"


def test_tool_accepts_complete_bundle(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_complete({"summary": "done", "metadata": dict(_COMPLETE)})
    assert json.loads(out)["ok"] is True
    assert _status(worker_env) in {"done", "review"}


def test_tool_legacy_completion_without_schema_version_is_exempt(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_complete({"summary": "done", "metadata": {"residual_risk": "none"}})
    assert json.loads(out)["ok"] is True
    assert _status(worker_env) in {"done", "review"}


# ---------------------------------------------------------------------------
# 2. claude-CLI path (_cmd_complete)
# ---------------------------------------------------------------------------


def test_cli_rejects_incomplete_bundle_in_flight(worker_env, capsys):
    from hermes_cli import kanban as kanban_cli

    args = argparse.Namespace(
        task_ids=[worker_env],
        summary="done",
        metadata=json.dumps({"schema_version": 1}),
        result=None,
    )
    rc = kanban_cli._cmd_complete(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing required field" in err
    assert "gates.exit_code" in err
    # IN-FLIGHT: no state change.
    assert _status(worker_env) == "running"


def test_cli_legacy_completion_is_exempt(worker_env):
    from hermes_cli import kanban as kanban_cli

    args = argparse.Namespace(
        task_ids=[worker_env],
        summary="done",
        metadata=json.dumps({"residual_risk": "none", "changed_files": ["a.py"]}),
        result=None,
    )
    rc = kanban_cli._cmd_complete(args)
    assert rc == 0
    assert _status(worker_env) in {"done", "review"}


# ---------------------------------------------------------------------------
# 3. Render guarantee in build_worker_context (end-to-end)
# ---------------------------------------------------------------------------


def test_build_worker_context_preserves_mandatory_bundle_fields(worker_env, monkeypatch):
    """A child reading a parent whose completion bundle is huge still sees the
    parent's mandatory fields — they are never truncated off the 4 KB cap."""
    # complete_task itself does NOT gate the bundle, so we can seed a parent run
    # carrying a deliberately huge (but otherwise valid) bundle directly.
    big_bundle = {
        "schema_version": 1,
        "gates": {"exit_code": 0},
        "AC": "all acceptance criteria covered",
        "residual_risk": "low — see disposition",
        "disposition": {"items": [{"blob": "z" * 80} for _ in range(300)]},
        "changed_files": ["module_%03d.py" % i for i in range(300)],
    }
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent-bundle", assignee="coder")
        child = kb.create_task(conn, title="child", assignee="writer", parents=[parent])
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="impl done", metadata=big_bundle)

        ctx = kb.build_worker_context(conn, child)
    finally:
        conn.close()

    assert "Parent task results" in ctx
    # The mandatory bundle fields survive the per-field truncation.
    assert '"schema_version"' in ctx
    assert '"residual_risk"' in ctx
    assert '"AC"' in ctx
    assert '"exit_code"' in ctx
