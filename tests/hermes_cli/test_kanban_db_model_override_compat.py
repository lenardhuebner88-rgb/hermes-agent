"""Regression tests for model_override provider-family compatibility.

Covers AC-1..AC-4 of t_82081236:

- AC-1: pure cross-family model_override is ignored at spawn; a
  model_override_incompatible event is recorded.
- AC-2: compatible model_override is still applied.
- AC-3: auto-retry escalation that picks a cross-family model stores a
  provider/model pair (not a poison-pill model-only override).
- AC-4: tasks without model_override keep lane routing.
"""

from __future__ import annotations

import json

from hermes_cli import kanban_db as kb


def _fake_lane_openai_codex(conn, profile):
    return {
        "worker_runtime": "hermes",
        "provider": "openai-codex",
        "model": "gpt-5.4",
    }


def test_spawn_identity_incompatible_model_override_falls_back_to_lane(
    kanban_home, monkeypatch
):
    """AC-1: model_override=claude-opus-4-8 with lane openai-codex is ignored."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn", _fake_lane_openai_codex
    )
    monkeypatch.setattr(
        kb,
        "_profile_model_provider_for_spawn",
        lambda home: ("openai-codex", "gpt-5.4", "hermes"),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="poison-pill", assignee="research")
        conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?",
            ("claude-opus-4-8", tid),
        )
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata, requested_provider, requested_model, model_source "
            "FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["route_provider"] == "openai-codex"
        assert meta["model"] == "gpt-5.4"
        assert meta["model_source"] == "lane"
        assert row["requested_provider"] == "openai-codex"
        assert row["requested_model"] == "gpt-5.4"
        assert row["model_source"] == "lane"

        events = [e for e in kb.list_events(conn, tid) if e.kind == "model_override_incompatible"]
        assert len(events) == 1
        payload = events[0].payload
        assert payload["model_override"] == "claude-opus-4-8"
        assert payload["provider"] == "openai-codex"
        assert payload["reason"] == "provider_family_mismatch"


def test_spawn_identity_compatible_model_override_is_applied(
    kanban_home, monkeypatch
):
    """AC-2: gpt-5.6-luna on openai-codex stays applied."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn", _fake_lane_openai_codex
    )
    monkeypatch.setattr(
        kb,
        "_profile_model_provider_for_spawn",
        lambda home: ("openai-codex", "gpt-5.4", "hermes"),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="compatible-override", assignee="research")
        conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?",
            ("gpt-5.6-luna", tid),
        )
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata, requested_provider, requested_model, model_source "
            "FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["route_provider"] == "openai-codex"
        assert meta["model"] == "gpt-5.6-luna"
        assert meta["model_source"] == "task_override"
        assert row["requested_model"] == "gpt-5.6-luna"
        assert row["model_source"] == "task_override"
        events = [e for e in kb.list_events(conn, tid) if e.kind == "model_override_incompatible"]
        assert events == []


def test_auto_retry_escalation_sets_provider_model_pair(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """AC-3: second auto-retry stores anthropic/<model> when lane is openai-codex."""
    base = 1_800_000_000
    monkeypatch.setattr(kb.time, "time", lambda: base)
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn", _fake_lane_openai_codex
    )
    monkeypatch.setattr(
        kb,
        "_profile_model_provider_for_spawn",
        lambda home: ("openai-codex", "gpt-5.4", "hermes"),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="blocked-escalation", assignee="research")
        conn.execute("UPDATE tasks SET auto_retry_count = 1 WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid)
        run = conn.execute(
            "SELECT requested_provider FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        assert run["requested_provider"] == "openai-codex"
        kb.block_task(conn, tid, reason="tool crashed")

        monkeypatch.setattr(kb.time, "time", lambda: base + 301)
        res = kb.dispatch_once(conn, auto_retry_blocked=True, max_spawn=0)

        assert res.auto_retried_blocked == [(tid, 2)]
        expected = f"anthropic/{kb.AUTO_RETRY_ESCALATION_MODEL}"
        row = conn.execute(
            "SELECT status, auto_retry_count, assignee, model_override FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["auto_retry_count"] == 2
        assert row["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE
        assert row["model_override"] == expected

        event = [e for e in kb.list_events(conn, tid) if e.kind == "auto_retried"][-1]
        assert event.payload["escalated"] is True
        assert event.payload["model_override"] == expected
        assert event.payload["assignee"] == kb.AUTO_RETRY_ESCALATION_PROFILE


def test_spawn_identity_without_model_override_uses_lane(
    kanban_home, monkeypatch
):
    """AC-4: no model_override keeps lane routing unchanged."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn", _fake_lane_openai_codex
    )
    monkeypatch.setattr(
        kb,
        "_profile_model_provider_for_spawn",
        lambda home: ("openai-codex", "gpt-5.4", "hermes"),
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no-override", assignee="research")
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata, requested_provider, requested_model, model_source "
            "FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["route_provider"] == "openai-codex"
        assert meta["model"] == "gpt-5.4"
        assert meta["model_source"] == "lane"
        assert row["requested_provider"] == "openai-codex"
        assert row["requested_model"] == "gpt-5.4"
        assert row["model_source"] == "lane"
        events = [e for e in kb.list_events(conn, tid) if e.kind == "model_override_incompatible"]
        assert events == []
