"""Query-budget and compactness tests for Flow-board chain summaries."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serve the dashboard router against the shared isolated kanban fixture."""
    home = tmp_path / ".hermes"
    home.mkdir()
    kb.init_db(db_path=home / "kanban.db")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_query_budget_test", plugin_file
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/kanban")
    app.state.kanban_plugin_module = module
    with module._board_cache_lock:
        module._board_cache.clear()
    return TestClient(app)


def _insert_two_task_chains(conn, *, start: int, count: int, status: str = "scheduled"):
    tasks = []
    links = []
    for number in range(start, start + count):
        source_id = f"t_chain_{number}_source"
        sink_id = f"t_chain_{number}_sink"
        tasks.extend(
            [
                (source_id, f"Chain {number} source", status, number * 2, None),
                (sink_id, f"Chain {number} sink", status, number * 2 + 1, None),
            ]
        )
        links.append((source_id, sink_id))

    with kb.write_txn(conn):
        conn.executemany(
            "INSERT INTO tasks (id, title, status, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            tasks,
        )
        conn.executemany(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", links
        )


def _count_board_selects(client, monkeypatch):
    """Count real SQLite SELECTs while rendering a fresh board payload."""
    module = client.app.state.kanban_plugin_module
    original_conn = module._conn
    statements: list[str] = []

    def traced_conn(*args, **kwargs):
        conn = original_conn(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with monkeypatch.context() as measurement_patch:
        measurement_patch.setattr(module, "_conn", traced_conn)
        with module._board_cache_lock:
            module._board_cache.clear()
        response = client.get(
            "/api/plugins/kanban/board",
            params={"done_limit": 20, "card_diagnostics": "summary", "card_body": "none"},
        )
    assert response.status_code == 200, response.text
    return sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_chain_summary_query_count_is_flat_from_five_to_150_chains(client, monkeypatch):
    """The tab must batch-load its summaries instead of querying per chain."""
    with kb.connect() as conn:
        _insert_two_task_chains(conn, start=0, count=5)
    five_chain_selects = _count_board_selects(client, monkeypatch)

    with kb.connect() as conn:
        _insert_two_task_chains(conn, start=5, count=145)
    one_hundred_fifty_chain_selects = _count_board_selects(client, monkeypatch)

    assert one_hundred_fifty_chain_selects <= five_chain_selects + 1, (
        "board SELECT count grew with the number of chains: "
        f"5={five_chain_selects}, 150={one_hundred_fifty_chain_selects}"
    )


def test_completed_chain_summaries_omit_station_detail(client):
    """Finished chains retain their aggregate but do not ship open-chain stations."""
    with kb.connect() as conn:
        _insert_two_task_chains(conn, start=999, count=1, status="done")

    response = client.get(
        "/api/plugins/kanban/board",
        params={"done_limit": 20, "card_diagnostics": "summary", "card_body": "none"},
    )
    assert response.status_code == 200, response.text
    chain = next(
        summary
        for summary in response.json()["chain_summaries"]
        if summary["root_id"] == "t_chain_999_sink"
    )

    assert chain["total"] == 2
    assert chain["stations"] == []
    assert chain["station_limit"] == 0
