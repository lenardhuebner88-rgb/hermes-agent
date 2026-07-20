from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import agent_questions as aq
from hermes_cli import pa_actions
from hermes_cli.pa_chat import PAStore
from hermes_cli.pa_reminders import create_reminder, due_reminders, mark_fired


@pytest.fixture
def reminder_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return hermes_home


def _future_iso(*, hours: int = 1) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


def test_reminder_payload_normalizes_future_iso_to_utc() -> None:
    normalized = aq.normalize_pa_action_payload(
        "reminders.create",
        {
            "due_at": "2999-01-02T10:30:00+01:00",
            "title": "  Termin  ",
            "body": "  Unterlagen mitnehmen  ",
        },
    )

    assert normalized == {
        "due_at": "2999-01-02T09:30:00.000000Z",
        "title": "Termin",
        "body": "Unterlagen mitnehmen",
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"due_at": "2020-01-01T00:00:00Z", "title": "Alt"}, "Zukunft"),
        ({"due_at": "morgen", "title": "Kaputt"}, "ISO-8601"),
        ({"due_at": "2999-01-01T00:00:00", "title": "Naiv"}, "Zeitzone"),
        ({"due_at": "2999-01-01T00:00:00Z"}, "title"),
        ({"due_at": "2999-01-01T00:00:00Z", "title": " "}, "nicht leer"),
        (
            {"due_at": "2999-01-01T00:00:00Z", "title": "x" * 201},
            "200 Zeichen",
        ),
        (
            {
                "due_at": "2999-01-01T00:00:00Z",
                "title": "Lang",
                "body": "x" * 501,
            },
            "500 Zeichen",
        ),
    ],
)
def test_reminder_payload_rejects_invalid_values(
    payload: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        aq.normalize_pa_action_payload("reminders.create", payload)


def test_reminder_store_selects_due_and_marks_once(reminder_home: Path) -> None:
    store = PAStore(reminder_home / "pa" / "pa.db")
    now = datetime.now(tz=timezone.utc)
    due_id = create_reminder(
        due_at_utc=(now - timedelta(minutes=1)).isoformat(),
        title="Fällig",
        store=store,
    )
    future_id = create_reminder(
        due_at_utc=(now + timedelta(hours=1)).isoformat(),
        title="Später",
        store=store,
    )

    assert [row["id"] for row in due_reminders(now, store=store)] == [due_id]
    assert mark_fired(due_id, now, store=store) is True
    assert mark_fired(due_id, now, store=store) is False
    assert due_reminders(now, store=store) == []
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, status FROM reminders ORDER BY title"
        ).fetchall()
    assert {row["id"]: row["status"] for row in rows} == {
        due_id: "fired",
        future_id: "pending",
    }


def test_reminder_is_written_only_after_approval_and_double_click_is_safe(
    reminder_home: Path,
) -> None:
    question_db = reminder_home / "question_events.db"
    store = PAStore()
    rejected_id = pa_actions.enqueue_pa_action(
        "reminders.create",
        {"due_at": _future_iso(), "title": "Nicht anlegen"},
        reason="Test reject",
        db_path=question_db,
    )
    rejected = aq.answer_question(rejected_id, "2", db_path=question_db)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0

    approved_id = pa_actions.enqueue_pa_action(
        "reminders.create",
        {"due_at": _future_iso(hours=2), "title": "Anlegen", "body": "Text"},
        reason="Test approve",
        db_path=question_db,
    )
    first = aq.answer_question(approved_id, "1", db_path=question_db)
    second = aq.answer_question(approved_id, "1", db_path=question_db)

    assert rejected["executed"] is False
    assert first["verified"] is True
    assert second == {"ok": False, "reason": "not-open"}
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT title, body, status FROM reminders"
        ).fetchall()
    assert [dict(row) for row in rows] == [
        {"title": "Anlegen", "body": "Text", "status": "pending"}
    ]


def test_execution_boundary_rejects_reminder_that_is_now_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden(_payload: dict[str, str]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setitem(pa_actions.ACTION_HANDLERS, "reminders.create", forbidden)
    with pytest.raises(ValueError, match="Zukunft"):
        pa_actions.execute_action(
            "reminders.create",
            {"due_at": "2020-01-01T00:00:00Z", "title": "Zu spät"},
        )
    assert called is False


def test_reminder_approval_card_uses_german_local_time() -> None:
    envelope = aq.build_pa_action_envelope(
        "reminders.create",
        {"due_at": "2999-01-02T09:30:00Z", "title": "Termin"},
        reason="Wichtig",
    )

    question = pa_actions.build_action_question(envelope)

    assert "02.01.2999 um 10:30 Uhr" in question
    assert "Titel: Termin" in question
    assert "Grund: Wichtig" in question
