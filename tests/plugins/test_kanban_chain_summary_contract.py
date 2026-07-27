"""Contract tests for the compact Flow-board chain summaries."""

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
        "hermes_dashboard_plugin_kanban_test", plugin_file
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/kanban")
    app.state.kanban_plugin_module = module
    sys.modules["hermes_dashboard_plugin_kanban_test"] = module
    with module._board_cache_lock:
        module._board_cache.clear()
    return TestClient(app)


def _insert_chain(conn, *, prefix: str, source_status: str, sink_status: str, source_title: str, planspec_source: str | None = None):
    source_id = f"t_{prefix}_source"
    sink_id = f"t_{prefix}_sink"
    with kb.write_txn(conn):
        conn.executemany(
            "INSERT INTO tasks (id, title, status, created_at, completed_at, planspec_source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    source_id,
                    source_title,
                    source_status,
                    1_000,
                    1_100 if source_status == "done" else None,
                    None,
                ),
                (
                    sink_id,
                    f"{source_title} gate",
                    sink_status,
                    1_001,
                    1_200 if sink_status == "done" else None,
                    planspec_source,
                ),
            ],
        )
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (source_id, sink_id),
        )
    return source_id, sink_id


def test_chain_summaries_publish_state_identity_age_and_aggregate_costs(
    client, monkeypatch
):
    """Each derived state is backed by a linked two-node chain."""
    with kb.connect() as conn:
        running = _insert_chain(
            conn,
            prefix="running",
            source_status="running",
            sink_status="todo",
            source_title="Running Plan",
            planspec_source="/plans/2026-07-27-running-plan.md",
        )
        held = _insert_chain(
            conn,
            prefix="held",
            source_status="scheduled",
            sink_status="scheduled",
            source_title="Held Plan",
        )
        partial = _insert_chain(
            conn,
            prefix="partial",
            source_status="done",
            sink_status="scheduled",
            source_title="Partial Plan",
        )
        finished = _insert_chain(
            conn,
            prefix="finished",
            source_status="done",
            sink_status="done",
            source_title="Finished Plan",
        )

    module = client.app.state.kanban_plugin_module
    monkeypatch.setattr(module, "_compute_task_diagnostics", lambda *args, **kwargs: {})
    monkeypatch.setattr(kb, "latest_summaries", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        kb,
        "batch_task_costs",
        lambda _conn, task_ids: {
            task_id: {
                "cost_usd": 0.25,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd_equivalent": 0.25,
                "cost_effective_usd": 0.25,
            }
            for task_id in task_ids
        },
    )
    monkeypatch.setattr(kb, "batch_active_review_stages", lambda *args, **kwargs: {})
    monkeypatch.setattr(kb, "vault_memory_links_for_task", lambda *args, **kwargs: [])
    monkeypatch.setattr(module.time, "time", lambda: 1_500)

    response = client.get(
        "/api/plugins/kanban/board",
        params={"done_limit": 20, "card_diagnostics": "summary", "card_body": "none"},
    )

    assert response.status_code == 200, response.text
    summaries = {row["root_id"]: row for row in response.json()["chain_summaries"]}
    assert summaries[running[1]] | {
        "state": "laeuft",
        "chain_identifier": "2026-07-27-running-plan",
        "state_age_seconds": 500,
        "cost_usd": 0.5,
    } == summaries[running[1]]
    assert summaries[held[1]] | {
        "state": "gehalten",
        "chain_identifier": "Held Plan",
        "state_age_seconds": 500,
        "cost_usd": 0.5,
    } == summaries[held[1]]
    assert summaries[partial[1]] | {
        "state": "angebrochen",
        "chain_identifier": "Partial Plan",
        "state_age_seconds": 400,
        "cost_usd": 0.5,
    } == summaries[partial[1]]
    assert summaries[finished[1]] | {
        "state": "fertig",
        "chain_identifier": "Finished Plan",
        "state_age_seconds": 300,
        "cost_usd": 0.5,
    } == summaries[finished[1]]


def test_chain_summary_stations_are_topological_capped_and_card_ready(
    client, monkeypatch
):
    """A branched five-plus-node chain stays linear, stable, and compact."""
    tasks = [
        ("t_station_source", "Source", "done", "researcher", 1_000, 1_010, 1_020),
        ("t_station_alpha", "Alpha", "running", "coder", 1_001, 1_011, None),
        ("t_station_beta", "Beta", "todo", "writer", 1_001, None, None),
        ("t_station_merge", "Merge", "reviewer", "verifier", 1_002, None, None),
        ("t_station_sink", "Sink", "scheduled", "operator", 1_003, None, None),
    ]
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO tasks "
                "(id, title, status, assignee, created_at, started_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                tasks,
            )
            conn.executemany(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                [
                    ("t_station_source", "t_station_alpha"),
                    ("t_station_source", "t_station_beta"),
                    ("t_station_alpha", "t_station_merge"),
                    ("t_station_beta", "t_station_merge"),
                    ("t_station_merge", "t_station_sink"),
                ],
            )

    module = client.app.state.kanban_plugin_module
    monkeypatch.setattr(module, "_CHAIN_SUMMARY_STATION_LIMIT", 4)
    monkeypatch.setattr(module, "_compute_task_diagnostics", lambda *args, **kwargs: {})
    monkeypatch.setattr(kb, "latest_summaries", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        kb,
        "batch_task_costs",
        lambda _conn, task_ids: {
            task_id: {
                "cost_usd": 0.5,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd_equivalent": 0.5,
                "cost_effective_usd": 0.5,
            }
            for task_id in task_ids
        },
    )
    monkeypatch.setattr(kb, "batch_active_review_stages", lambda *args, **kwargs: {})
    monkeypatch.setattr(kb, "vault_memory_links_for_task", lambda *args, **kwargs: [])
    monkeypatch.setattr(module.time, "time", lambda: 1_030)

    response = client.get(
        "/api/plugins/kanban/board",
        params={"done_limit": 20, "card_diagnostics": "summary", "card_body": "none"},
    )

    assert response.status_code == 200, response.text
    summary = next(
        row
        for row in response.json()["chain_summaries"]
        if row["root_id"] == "t_station_sink"
    )
    assert summary["total"] == 5
    assert summary["station_limit"] == 4
    assert [station["id"] for station in summary["stations"]] == [
        "t_station_source",
        "t_station_alpha",
        "t_station_beta",
        "t_station_merge",
    ]
    second_summary = next(
        row
        for row in client.get(
        "/api/plugins/kanban/board",
        params={"done_limit": 20, "card_diagnostics": "summary", "card_body": "none"},
        ).json()["chain_summaries"]
        if row["root_id"] == "t_station_sink"
    )
    assert summary["stations"] == second_summary["stations"]
    assert all(
        set(station) == {
            "id",
            "title",
            "status",
            "lane",
            "runtime_seconds",
            "cost_usd",
            "started_at",
            "completed_at",
        }
        for station in summary["stations"]
    )
    assert summary["stations"][0] == {
        "id": "t_station_source",
        "title": "Source",
        "status": "done",
        "lane": "researcher",
        "runtime_seconds": 10,
        "cost_usd": 0.5,
        "started_at": 1_010,
        "completed_at": 1_020,
    }
