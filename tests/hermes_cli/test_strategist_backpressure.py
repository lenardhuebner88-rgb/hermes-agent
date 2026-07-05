"""STRATEGIST-BACKPRESSURE-S1 — deterministic ingest pre-gate on the held queue.

The operator is the pipeline's bottleneck (funnel 2026-07-05: 187 proposals,
36 released, 178 archived): when >= max_held undecided held roots wait,
propose/harvest ingest self-skips like the budget self-skip; dry-run stays
available so the Opus wrapper keeps its context step.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db, strategist


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Isolated kanban DB + vision state redirected away from live state."""
    monkeypatch.setenv("HERMES_VISION_STATE_DIR", str(tmp_path / "state"))
    c = kanban_db.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _held_root(conn, tid, created_at=1_000):
    """An undecided held strategist root (event payload shape as in the live
    DB, sampled 2026-07-06)."""
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_by, created_at) "
        "VALUES (?, ?, 'scheduled', 'strategist-cron', ?)",
        (tid, f"lever {tid}", created_at),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'created', '{\"by\": \"strategist-cron\"}', ?)",
        (tid, created_at),
    )
    conn.commit()


def _no_budget_skip(monkeypatch):
    monkeypatch.setattr(
        strategist, "check_budget",
        lambda **kw: {"skip": False, "reason": None, "used_percent": 0.0},
    )


def test_below_threshold_no_skip(conn):
    _held_root(conn, "t_h1")
    bp = strategist._held_backpressure(conn, max_held=2)
    assert bp == {"skip": False, "held_open": 1, "max_held": 2, "reason": None}


def test_at_threshold_skips_with_reason(conn):
    _held_root(conn, "t_h1")
    _held_root(conn, "t_h2")
    bp = strategist._held_backpressure(conn, max_held=2)
    assert bp["skip"] is True
    assert bp["held_open"] == 2
    assert "backpressure" in bp["reason"]


def test_zero_disables_gate(conn):
    _held_root(conn, "t_h1")
    bp = strategist._held_backpressure(conn, max_held=0)
    assert bp["skip"] is False
    assert bp["held_open"] is None


def test_decided_roots_do_not_count(conn):
    _held_root(conn, "t_h1")
    _held_root(conn, "t_h2")
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES ('t_h2', 'freigabe_vetoed', '{\"author\": \"operator\"}', 2000)"
    )
    conn.commit()
    bp = strategist._held_backpressure(conn, max_held=2)
    assert bp["skip"] is False
    assert bp["held_open"] == 1


def test_propose_ingest_self_skips_under_backpressure(conn, tmp_path, monkeypatch):
    for i in range(strategist.BACKPRESSURE_MAX_HELD_DEFAULT):
        _held_root(conn, f"t_h{i}")
    _no_budget_skip(monkeypatch)
    result = strategist.propose(
        conn=conn, out_dir=tmp_path / "specs", do_ingest=True
    )
    assert result["skipped"] is True
    assert result["reason"].startswith("backpressure")
    assert result["held_open"] == strategist.BACKPRESSURE_MAX_HELD_DEFAULT
    assert result["ingested"] == []


def test_propose_dry_run_not_gated(conn, tmp_path, monkeypatch):
    for i in range(strategist.BACKPRESSURE_MAX_HELD_DEFAULT):
        _held_root(conn, f"t_h{i}")
    _no_budget_skip(monkeypatch)
    result = strategist.propose(
        conn=conn, out_dir=tmp_path / "specs", do_ingest=False
    )
    assert result.get("skipped") is not True
