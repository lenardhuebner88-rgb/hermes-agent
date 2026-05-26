from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.control_plane.gate_decisions import EnforceTimeout


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, *, worker_pid=12345, claim_expires=None):
    tid = kb.create_task(conn, title="running", assignee="alice")
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, tid, claimer=f"{host}:worker")
    assert claimed is not None
    kb._set_worker_pid(conn, tid, worker_pid)
    if claim_expires is not None:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (claim_expires, tid),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (claim_expires, kb.latest_run(conn, tid).id),
        )
    return tid


def _parity_events(conn, tid):
    return [
        e for e in kb.list_events(conn, tid)
        if e.kind == "gate_decision_parity"
    ]


def test_dispatch_shadow_parity_suppresses_matching_keep_running(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    with kb.connect() as conn:
        tid = _running_task(conn)
        kb.dispatch_once(conn, spawn_fn=lambda *_args: None)
        assert _parity_events(conn, tid) == []


def test_dispatch_shadow_parity_suppresses_matching_claim_extension(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    with kb.connect() as conn:
        tid = _running_task(conn, claim_expires=1)
        kb.dispatch_once(conn, spawn_fn=lambda *_args: None)
        assert _parity_events(conn, tid) == []
        assert any(e.kind == "claim_extended" for e in kb.list_events(conn, tid))


def test_dispatch_shadow_parity_emits_on_claim_reclaim_vs_crash_priority(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("pid_not_alive", None))
    with kb.connect() as conn:
        tid = _running_task(conn, claim_expires=1)
        kb.dispatch_once(conn, spawn_fn=lambda *_args: None)
        parity = _parity_events(conn, tid)
        assert len(parity) == 1
        payload = parity[0].payload
        assert payload["match"] is False
        assert payload["shadow_decision"]["action"] == "classify_crash"
        assert payload["ticker_decision"]["action"] == "reclaim_stale"


def test_dispatch_shadow_parity_emits_on_shadow_only_decision(kanban_home, monkeypatch):
    def fake_shadow(conn, *, now, stale_timeout_seconds, failure_limit):
        return [
            EnforceTimeout(
                "t_fake",
                99,
                elapsed_seconds=120,
                limit_seconds=60,
                will_block=False,
            )
        ]

    monkeypatch.setattr(kb, "_gate_shadow_decisions_for_running", fake_shadow)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="fake target")
        # Make the fake decision point at a real task id so event emission can
        # satisfy the task_events foreign-key-free but UI-expected task lookup.
        def fake_shadow_real(conn, *, now, stale_timeout_seconds, failure_limit):
            return [
                EnforceTimeout(
                    tid,
                    99,
                    elapsed_seconds=120,
                    limit_seconds=60,
                    will_block=False,
                )
            ]

        monkeypatch.setattr(kb, "_gate_shadow_decisions_for_running", fake_shadow_real)
        kb.dispatch_once(conn, spawn_fn=lambda *_args: None)
        parity = _parity_events(conn, tid)
        assert len(parity) == 1
        assert parity[0].payload["ticker_decision"] is None


def test_dispatch_shadow_parity_ignores_gave_up_when_crash_event_matches(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0))
    with kb.connect() as conn:
        tid = _running_task(conn)
        kb.dispatch_once(conn, spawn_fn=lambda *_args: None)
        assert _parity_events(conn, tid) == []
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "protocol_violation" in kinds
        assert "gave_up" in kinds
