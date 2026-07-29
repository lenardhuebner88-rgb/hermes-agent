"""Write-time classification for operator-escalation events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_escalation_class as escalation


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_enum_is_exactly_the_existing_heiler_vocabulary():
    assert escalation.ESCALATION_CLASSES == frozenset(kb.HEILER_CLASSES)


def test_append_event_freezes_deterministic_escalation_class(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="capacity hold", assignee="coder")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "operator_escalation",
                {
                    "why_now": "iteration budget exhausted",
                    "evidence": {
                        "trigger_outcome": "iteration_budget_exhausted",
                    },
                },
            )
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'operator_escalation'",
            (task_id,),
        ).fetchone()
        payload = json.loads(row["payload"])

    assert payload["escalation_class"] == "capacity"
    assert kb._classify_escalation_payload(payload)[0] == "capacity"


def test_valid_persisted_class_wins_and_invalid_value_is_replaced():
    preserved = escalation.materialize_event_payload(
        "operator_escalation",
        {
            "escalation_class": "conflict",
            "why_now": "timeout text must not rewrite history",
        },
        classifier=lambda _payload: ("transient", {}),
    )
    replaced = escalation.materialize_event_payload(
        "operator_escalation",
        {"escalation_class": "new-unregistered-value"},
        classifier=lambda _payload: ("real-bug", {}),
    )

    assert preserved["escalation_class"] == "conflict"
    assert replaced["escalation_class"] == "real-bug"
    frozen_class, evidence = kb._classify_escalation_payload(preserved)
    assert frozen_class == "conflict"
    assert evidence["matched"] == "default"


def test_non_escalation_events_remain_unchanged():
    payload = {"why_now": "not an escalation"}
    assert escalation.materialize_event_payload(
        "completed",
        payload,
        classifier=lambda _payload: ("real-bug", {}),
    ) is payload


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_persisted_escalation_class_rejects_non_dict():
    """Kill remove_guard L33: non-dict payload must return None, not raise."""
    assert escalation.persisted_escalation_class("not-a-dict") is None
    assert escalation.persisted_escalation_class(42) is None
    assert escalation.persisted_escalation_class(None) is None


def test_classification_without_classifier_returns_tuple():
    """Kill return_none L59: with no classifier, must return (value, evidence)."""
    result = escalation.persisted_escalation_classification(
        {"escalation_class": "transient"},
    )
    assert result is not None
    value, evidence = result
    assert value == "transient"
    assert evidence["matched"] == "transient"
    assert evidence["signal_source"] == "persisted_escalation_class"
