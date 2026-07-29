"""Tests for agent-question web-push (I3): payload, debounce, age, visibility."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import agent_question_push as aqp
from hermes_cli import agent_questions as aq


@pytest.fixture()
def qdb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Clear process-local push state between tests.
    db = home / "question_events.db"
    aqp.reset_push_state_for_tests(db_path=db)
    yield db
    aqp.reset_push_state_for_tests(db_path=db)


def _insert_open(
    db: Path,
    *,
    pane: str = "%1",
    question: str = "Deploy now?",
    kind: str = "claude",
    now: float | None = None,
    source: str = "scrape",
) -> int:
    ts = now if now is not None else time.time()
    if source == "hook":
        r = aq.ingest_hook_event(
            {
                "pane_id": pane,
                "session": "s",
                "window": "w",
                "kind": kind,
                "cwd": "/tmp",
                "question_text": question,
                "options": [
                    {"nr": 1, "label": "Yes", "recommended": True},
                    {"nr": 2, "label": "No", "recommended": False},
                ],
                "hook_key": f"hk-{pane}-{ts}",
            },
            db_path=db,
            now=ts,
        )
        assert r["ok"] is True
        return int(r["id"])
    n_super, new_id = aq.supersede_and_insert(
        session="s",
        window="w",
        pane_id=pane,
        fingerprint=f"fp-{pane}-{ts}",
        question_text=question,
        options=[
            {"nr": 1, "label": "Yes", "recommended": True},
            {"nr": 2, "label": "No", "recommended": False},
        ],
        kind=kind,
        cwd="/tmp",
        source=source,
        db_path=db,
        now=ts,
    )
    assert new_id is not None
    return int(new_id)


def test_build_question_push_payload_single() -> None:
    payload = aqp.build_question_push_payload(
        [
            {
                "id": 42,
                "kind": "claude",
                "question_text": "Allow network access for this tool?",
                "status": "open",
            }
        ]
    )
    assert payload is not None
    assert payload["schema"] == "hermes-control-push-v1"
    assert payload["title"] == "Frage von claude"
    assert "Allow network" in payload["body"]
    assert payload["url"] == "/control/agent-terminals?question=42"
    assert payload["tag"] == "agent-question-42"
    assert payload["task_id"] == "question-42"


def test_build_question_push_payload_bundled() -> None:
    payload = aqp.build_question_push_payload(
        [
            {"id": 1, "kind": "claude", "question_text": "Q1?", "status": "open"},
            {"id": 2, "kind": "codex", "question_text": "Q2 longer question text", "status": "open"},
        ]
    )
    assert payload is not None
    assert payload["title"] == "2 offene Fragen"
    # Deep-link prefers newest (highest id).
    assert payload["url"] == "/control/agent-terminals?question=2"
    assert payload["tag"] == "agent-question-bundle"


def test_debounce_two_events_one_bundled_push(qdb: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two events within 60s → first push immediate, second held then one flush.

    Plan wording: max 1 push/minute, bundling. When the second arrives while the
    debounce window is open it is queued; flush after 60s sends the remainder
    (or a multi-event bundle when both are still pending).
    """
    sent: list[dict[str, Any]] = []

    def _fake_send(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"enabled": True, "sent": 1, "removed": 0, "failed": 0}

    monkeypatch.setattr(aqp, "_send_fn", _fake_send)

    t0 = 1_700_000_000.0
    # supersede_and_insert does not auto-push (only poll_once / hook ingest do).
    id1 = _insert_open(qdb, pane="%10", question="First?", now=t0)
    id2 = _insert_open(qdb, pane="%11", question="Second?", now=t0 + 1.0)

    r1 = aqp.maybe_push_question(id1, db_path=qdb, now=t0 + 2.0)
    assert r1.get("queued") is True
    assert len(sent) == 1
    assert sent[0]["url"].endswith(f"question={id1}")

    # Second event within 60s → debounced, not a second push yet.
    r2 = aqp.maybe_push_question(id2, db_path=qdb, now=t0 + 10.0)
    assert r2.get("queued") is True
    assert r2.get("reason") == "debounced"
    assert len(sent) == 1

    # Both still open: re-queue id1 as well and flush as a bundle after window.
    aqp.maybe_push_question(id1, db_path=qdb, now=t0 + 11.0)
    flush = aqp.flush_pending_pushes(now=t0 + 70.0, db_path=qdb, force=True)
    assert flush.get("sent") is True
    assert flush.get("n") == 2
    assert len(sent) == 2
    assert sent[1]["title"] == "2 offene Fragen"
    assert "question=" in sent[1]["url"]


def test_age_guard_skips_old_events(qdb: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        aqp,
        "_send_fn",
        lambda payload: sent.append(payload) or {"enabled": True, "sent": 1},
    )
    t0 = 1_700_000_000.0
    old_id = _insert_open(
        qdb, pane="%20", question="Ancient?", now=t0 - (3 * 3600)
    )
    r = aqp.maybe_push_question(old_id, db_path=qdb, now=t0)
    assert r.get("queued") is False
    assert r.get("reason") == "too-old"
    assert sent == []


def test_visibility_gate_skips_when_heartbeat_fresh(
    qdb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        aqp,
        "_send_fn",
        lambda payload: sent.append(payload) or {"enabled": True, "sent": 1},
    )
    t0 = 1_700_000_000.0
    eid = _insert_open(qdb, pane="%30", question="Visible tab?", now=t0)
    aqp.set_last_visible_ts(now=t0, db_path=qdb)
    r = aqp.maybe_push_question(eid, db_path=qdb, now=t0 + 5.0)
    assert r.get("queued") is False
    assert r.get("reason") == "visible"
    assert sent == []

    # After visibility window expires, push proceeds.
    r2 = aqp.maybe_push_question(eid, db_path=qdb, now=t0 + 40.0)
    assert r2.get("queued") is True
    assert len(sent) == 1


def test_drain_pending_on_start_rearms_stranded_bundle(
    qdb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending ids survive a restart, the debounce timer does not — the startup
    drain must re-arm the flush so the bundle is not stranded (review m2)."""
    eid = _insert_open(qdb, source="hook")
    # Simulate restart inside the debounce window: pending persisted, no timer.
    aq.set_meta("push_pending_ids", f"[{eid}]", db_path=qdb)
    aq.set_meta("push_last_ts", "0", db_path=qdb)

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(aqp, "_send_fn", lambda payload: sent.append(payload) or {"sent": 1})

    scheduled: list[float] = []
    monkeypatch.setattr(
        aqp,
        "_schedule_flush",
        lambda delay_s, *, db_path: scheduled.append(delay_s)
        or aqp.flush_pending_pushes(db_path=db_path),
    )
    assert aqp.drain_pending_on_start(db_path=qdb) is True
    assert scheduled == [1.0]
    assert len(sent) == 1
    assert f"question={eid}" in sent[0]["url"]

    # Empty pending → no re-arm.
    assert aqp.drain_pending_on_start(db_path=qdb) is False
    assert scheduled == [1.0]


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_bundle_body_uses_primary_question_text() -> None:
    """Kill bool_op_swap L163: or->and would replace the actual question
    text with the '{n} neue Fragen' fallback in bundled payloads."""
    payload = aqp.build_question_push_payload(
        [
            {"id": 1, "kind": "claude", "question_text": "First?", "status": "open"},
            {"id": 2, "kind": "codex", "question_text": "Second question?", "status": "open"},
        ]
    )
    assert payload is not None
    assert payload["title"] == "2 offene Fragen"
    # Body must contain the primary (highest-id) question text, not the fallback.
    assert "Second question?" in payload["body"]
    assert "neue Fragen" not in payload["body"]


def test_send_fn_import_failure_returns_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill boolean_flip L181: False->True would report enabled=True
    when the sender import actually fails."""
    import builtins
    real_import = builtins.__import__

    def _fail_import(name, *args, **kwargs):
        if "plugin_api" in name:
            raise ImportError("no plugin")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_import)
    result = aqp._send_payload({"test": True})
    assert result["enabled"] is False
    assert result["sent"] == 0


def test_flush_skips_old_events_by_ts(qdb: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill bool_op_swap L225: or->and would make _iso_to_epoch receive ''
    for valid timestamps, returning None and letting old events through."""
    t0 = 1_700_000_000.0
    old_id = _insert_open(qdb, pane="%30", question="Stale?", now=t0 - (3 * 3600))
    aqp._save_pending([old_id], db_path=qdb, now=t0)
    aq.set_meta("push_last_ts", "0", db_path=qdb)

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(aqp, "_send_fn", lambda payload: sent.append(payload) or {"sent": 1})

    result = aqp.flush_pending_pushes(now=t0, db_path=qdb)
    # Old event must be filtered out — nothing to send.
    assert result.get("sent") is False or result.get("events_sent", 0) == 0
    assert sent == []


def test_flush_first_push_sends_immediately(qdb: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill bool_op_swap L359: or->and would block the very first push
    (last<=0) because elapsed>=DEBOUNCE_S is False when last==0."""
    t0 = 1_700_000_000.0
    eid = _insert_open(qdb, pane="%40", question="Fresh?", now=t0)
    aqp._save_pending([eid], db_path=qdb, now=t0)
    # Ensure no prior push timestamp (first push ever).
    aq.set_meta("push_last_ts", "0", db_path=qdb)

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(aqp, "_send_fn", lambda payload: sent.append(payload) or {"sent": 1})

    result = aqp.flush_pending_pushes(now=t0, db_path=qdb)
    assert len(sent) == 1
    assert result.get("sent") is True or result.get("events_sent", 1) >= 1
