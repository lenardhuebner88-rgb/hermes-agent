"""Contract tests for chain identity enrichment in the Fleet board payload."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_chain_identity_test", plugin_file
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, _kanban_plugin_home_template):
    home = tmp_path / ".hermes"
    shutil.copytree(_kanban_plugin_home_template, home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _task_payloads(payload):
    return {
        task["id"]: task
        for column in payload["columns"]
        for task in column["tasks"]
    }


def test_board_exposes_deduplicated_chain_identity_and_member_positions(client):
    conn = kb.connect()
    try:
        first = kb.create_task(conn, title="Prepare release", assignee="coder")
        middle = kb.create_task(
            conn, title="Run verification", assignee="coder", parents=[first]
        )
        gate = kb.create_task(
            conn, title="Release gate", assignee="coder", parents=[middle]
        )
        standalone = kb.create_task(conn, title="Unrelated task", assignee="coder")
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id = ?",
            ("/plans/2026-07-27-fleet-chain.md", first),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/plugins/kanban/board")

    assert response.status_code == 200
    payload = response.json()
    cards = _task_payloads(payload)
    assert set(cards) == {first, middle, gate, standalone}
    assert payload["chain_identities"] == {gate: "2026-07-27-fleet-chain"}
    assert cards[first]["chain"] == {
        "identity_id": gate,
        "position": 1,
        "total": 3,
    }
    assert cards[middle]["chain"] == {
        "identity_id": gate,
        "position": 2,
        "total": 3,
    }
    assert cards[gate]["chain"] == {
        "identity_id": gate,
        "position": 3,
        "total": 3,
    }
    assert "chain" not in cards[standalone]


def test_board_uses_first_member_title_when_chain_has_no_planspec_source(client):
    conn = kb.connect()
    try:
        first = kb.create_task(conn, title="Human-readable first step", assignee="coder")
        gate = kb.create_task(conn, title="Gate", assignee="coder", parents=[first])
    finally:
        conn.close()

    payload = client.get("/api/plugins/kanban/board").json()

    assert payload["chain_identities"] == {gate: "Human-readable first step"}


def test_done_page_keeps_visible_chain_member_context_from_full_board(client):
    """Done pagination cannot truncate a visible member's chain identity."""
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
                [
                    ("t_unrelated_done", "Earlier completed task", "done", 1),
                    ("t_paged_source", "Paged source", "done", 100),
                    ("t_paged_middle", "Paged middle", "done", 101),
                    ("t_paged_sink", "Visible sink", "scheduled", 102),
                ],
            )
            conn.executemany(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                [
                    ("t_paged_source", "t_paged_middle"),
                    ("t_paged_middle", "t_paged_sink"),
                ],
            )

    response = client.get(
        "/api/plugins/kanban/board",
        params={"done_limit": 1, "card_diagnostics": "summary", "card_body": "none"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    cards = _task_payloads(payload)
    assert set(cards) == {"t_unrelated_done", "t_paged_sink"}
    assert cards["t_paged_sink"]["chain"] == {
        "identity_id": "t_paged_sink",
        "position": 3,
        "total": 3,
    }
    summary = next(
        row
        for row in payload["chain_summaries"]
        if row["root_id"] == "t_paged_sink"
    )
    assert summary["total"] == cards["t_paged_sink"]["chain"]["total"]


def test_done_page_keeps_position_for_visible_member_of_finished_chain(client):
    """Finished-chain stations stay hidden, but visible cards keep positions."""
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
                [
                    ("t_earlier_done", "Earlier completed task", "done", 1),
                    ("t_finished_source", "Finished source", "done", 100),
                    ("t_finished_middle", "Finished middle", "done", 101),
                    ("t_finished_sink", "Finished sink", "done", 102),
                ],
            )
            conn.executemany(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                [
                    ("t_finished_source", "t_finished_middle"),
                    ("t_finished_middle", "t_finished_sink"),
                ],
            )

    response = client.get(
        "/api/plugins/kanban/board",
        params={"done_limit": 2, "card_diagnostics": "summary", "card_body": "none"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    cards = _task_payloads(payload)
    assert cards["t_finished_source"]["chain"] == {
        "identity_id": "t_finished_sink",
        "position": 1,
        "total": 3,
    }
    summary = next(
        row
        for row in payload["chain_summaries"]
        if row["root_id"] == "t_finished_sink"
    )
    assert summary["state"] == "fertig"
    assert summary["stations"] == []
