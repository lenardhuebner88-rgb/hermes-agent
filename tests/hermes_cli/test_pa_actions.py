from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermes_cli import agent_questions as aq
from hermes_cli import pa_actions
from hermes_cli import pa_chat
from hermes_cli.agent_terminals import InvalidTarget, TmuxAgentSessionService


@pytest.fixture
def isolated_action_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return hermes_home


@pytest.fixture
def question_db(isolated_action_home: Path) -> Path:
    return isolated_action_home / "question_events.db"


def _enqueue(question_db: Path) -> int:
    return pa_actions.enqueue_pa_action(
        "tmux.send_keys",
        {"session": "work", "window": "kimi", "keys": "weiter"},
        reason="Kimi soll fortfahren",
        db_path=question_db,
    )


def test_confirm_executes_once_and_persists_event_and_thread_evidence(
    question_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = _enqueue(question_db)
    calls: list[dict[str, str]] = []

    def fake_handler(payload: dict[str, str]) -> dict[str, Any]:
        calls.append(payload)
        return {
            "ok": True,
            "exit": 0,
            "payload": {"bytes": 6},
            "pane_tail": "Kimi: working",
        }

    monkeypatch.setitem(pa_actions.ACTION_HANDLERS, "tmux.send_keys", fake_handler)

    first = aq.answer_question(event_id, "1", db_path=question_db)
    second = aq.answer_question(event_id, "1", db_path=question_db)

    assert first["ok"] is True
    assert first["executed"] is True
    assert first["verified"] is True
    assert second == {"ok": False, "reason": "not-open"}
    assert calls == [{"session": "work", "window": "kimi", "keys": "weiter"}]

    answered = aq.list_question_events(status="answered", db_path=question_db)
    assert len(answered) == 1
    evidence = answered[0]["action_result"]
    assert evidence["status"] == "succeeded"
    assert evidence["executed"] is True
    assert evidence["result"]["exit"] == 0
    assert evidence["result"]["pane_tail"] == "Kimi: working"

    thread = pa_chat.PAStore().recent_messages()
    assert len(thread) == 1
    assert thread[0]["role"] == "assistant"
    assert thread[0]["engine"] == "pa-executor"
    assert "tmux.send_keys" in thread[0]["content"]
    assert "Kimi: working" in thread[0]["content"]


def test_reject_answers_without_executing(
    question_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = _enqueue(question_db)

    def forbidden(_payload: dict[str, str]) -> dict[str, Any]:
        raise AssertionError("rejected action must not execute")

    monkeypatch.setitem(pa_actions.ACTION_HANDLERS, "tmux.send_keys", forbidden)

    result = aq.answer_question(event_id, "2", db_path=question_db)

    assert result["ok"] is True
    assert result["executed"] is False
    event = aq.list_question_events(status="answered", db_path=question_db)[0]
    assert event["answer"] == "2"
    assert event["action_result"]["status"] == "rejected"
    assert event["action_result"]["result"]["executed"] is False


def test_double_confirm_http_is_409_and_does_not_execute_twice(
    question_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import hermes_cli.web_server as web_server

    event_id = _enqueue(question_db)
    calls = 0

    def fake_handler(_payload: dict[str, str]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True, "exit": 0, "payload": {"sent": True}}

    monkeypatch.setattr(aq, "question_events_db_path", lambda: question_db)
    monkeypatch.setitem(pa_actions.ACTION_HANDLERS, "tmux.send_keys", fake_handler)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    with TestClient(web_server.app) as client:
        first = client.post(
            f"/api/agent-questions/{event_id}/answer",
            json={"answer": "1", "answered_by": "operator"},
            headers=headers,
        )
        second = client.post(
            f"/api/agent-questions/{event_id}/answer",
            json={"answer": "1", "answered_by": "operator"},
            headers=headers,
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == {"ok": False, "reason": "not-open"}
    assert calls == 1


def test_enqueue_deduplicates_open_payload_and_keeps_first_reason(
    question_db: Path,
) -> None:
    first = pa_actions.enqueue_pa_action(
        "kanban.nudge",
        {"card_id": "t_123", "reason": "Bitte prüfen"},
        reason="Board wirkt still",
        db_path=question_db,
    )
    second = pa_actions.enqueue_pa_action(
        "kanban.nudge",
        {"reason": "Bitte prüfen", "card_id": "t_123"},
        reason="Anderer Erklärungstext",
        db_path=question_db,
    )

    assert second == first
    events = aq.list_question_events(status="open", db_path=question_db)
    assert len(events) == 1
    assert events[0]["action_payload"]["reason"] == "Board wirkt still"
    assert events[0]["options"] == pa_actions.PA_ACTION_OPTIONS


@pytest.mark.parametrize(
    ("category", "payload"),
    [
        ("tmux.send_keys", {"session": "work", "window": "kimi"}),
        ("kanban.unblock", {"card_id": "t_1", "force": True}),
        ("kanban.unknown", {"card_id": "t_1"}),
    ],
)
def test_enqueue_rejects_invalid_payload_without_row(
    question_db: Path,
    category: str,
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        pa_actions.enqueue_pa_action(
            category,
            payload,
            reason="invalid",
            db_path=question_db,
        )
    assert aq.list_question_events(status="open", db_path=question_db) == []


class _FakeTmuxService:
    validate_name = staticmethod(TmuxAgentSessionService.validate_name)

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def send_keys(self, session: str, window: str, keys: str) -> None:
        self.calls.append(("send_keys", session, window, keys))

    def interrupt(self, session: str, window: str) -> None:
        self.calls.append(("interrupt", session, window))

    def capture(self, session: str, window: str, *, start: int, log: bool) -> str:
        self.calls.append(("capture", session, window, start, log))
        return "tail"


def test_tmux_handlers_reuse_target_validation_and_existing_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTmuxService()
    monkeypatch.setattr(pa_actions, "_new_tmux_service", lambda: service)

    sent = pa_actions.execute_action(
        "tmux.send_keys",
        {"session": "work", "window": "kimi", "keys": "weiter"},
    )
    interrupted = pa_actions.execute_action(
        "tmux.interrupt",
        {"session": "work", "window": "codex"},
    )

    assert sent["ok"] is True and sent["pane_tail"] == "tail"
    assert interrupted["ok"] is True and interrupted["payload"]["signal"] == "C-c"
    assert service.calls == [
        ("send_keys", "work", "kimi", "weiter"),
        ("capture", "work", "kimi", -50, False),
        ("interrupt", "work", "codex"),
        ("capture", "work", "codex", -50, False),
    ]

    with pytest.raises(InvalidTarget, match="invalid session"):
        pa_actions.execute_action(
            "tmux.interrupt",
            {"session": "../work", "window": "codex"},
        )


def test_registry_covers_every_v1_category() -> None:
    expected = {
        "tmux.send_keys",
        "tmux.interrupt",
        "kanban.unblock",
        "kanban.nudge",
        "kanban.hold",
        "kanban.resume",
        "kanban.kill",
        "kanban.release",
        "planspec.ingest",
        "loops.start_pack",
        "loops.status",
    }
    assert set(pa_actions.ACTION_HANDLERS) == expected
    assert set(aq._PA_ACTION_SCHEMAS) == expected
