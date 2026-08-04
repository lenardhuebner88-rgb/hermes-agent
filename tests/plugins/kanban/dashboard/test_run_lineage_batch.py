"""Production-path coverage for batched task-run lineage resolution."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


_LINEAGE_KEYS = ("run_role", "run_role_label", "run_role_source")


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[4]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_run_lineage_batch_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plugin_module():
    return _load_plugin_module()


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
def client(plugin_module, kanban_home):
    app = FastAPI()
    app.include_router(plugin_module.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _lineage(run: dict) -> dict[str, str]:
    return {key: run[key] for key in _LINEAGE_KEYS}


def _insert_run_fixture() -> tuple[str, list[int]]:
    """Persist the same task_runs/task_events shapes written by live claims."""
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Lineage batch fixture", assignee="coder")
        run_ids: list[int] = []
        with kb.write_txn(conn):
            for index in range(53):
                run_id = int(
                    conn.execute(
                        "INSERT INTO task_runs ("
                        "task_id, profile, status, started_at, ended_at, outcome, metadata, "
                        "requested_provider, requested_model, active_provider, active_model, "
                        "model_state, model_source, model_observed_at"
                        ") VALUES (?, 'coder', 'done', ?, ?, 'completed', ?, "
                        "'openai-codex', 'gpt-5.6-sol', 'openai-codex', 'gpt-5.6-sol', "
                        "'confirmed', 'worker_start', ?)",
                        (
                            task_id,
                            1_700_000_000 + index,
                            1_700_000_100 + index,
                            json.dumps({"worker_session_id": f"fixture-session-{index}"}),
                            1_700_000_000 + index,
                        ),
                    ).lastrowid
                )
                run_ids.append(run_id)

                if index < 50:
                    kb._append_event(
                        conn,
                        task_id,
                        "claimed",
                        {
                            "lock": f"fixture-lock-{index}",
                            "expires": 1_700_003_600 + index,
                            "run_id": run_id,
                            "source_status": "review" if index % 2 == 0 else "ready",
                        },
                        run_id=run_id,
                    )

            # The latest claimed event wins for a run, matching the live retry history.
            kb._append_event(
                conn,
                task_id,
                "claimed",
                {"run_id": run_ids[0], "source_status": "ready"},
                run_id=run_ids[0],
            )
            # Preserve the legacy decoder's distinctions: missing, malformed, and empty.
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'claimed', '{malformed', ?)",
                (task_id, run_ids[51], 1_700_000_200),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'claimed', NULL, ?)",
                (task_id, run_ids[52], 1_700_000_201),
            )
        return task_id, run_ids
    finally:
        conn.close()


def test_task_detail_batches_claimed_lineage_and_preserves_results(
    client,
    plugin_module,
    monkeypatch,
):
    task_id, run_ids = _insert_run_fixture()

    # Control path: no resolver means the pre-batch, one-query-per-run behavior.
    conn = kb.connect()
    try:
        rows = kb.list_runs(conn, task_id)
        legacy_model_resolver = plugin_module._LegacyModelRouteResolver(
            conn, list(rows), board=kb.DEFAULT_BOARD
        )
        expected = [
            _lineage(
                plugin_module._run_dict(
                    conn,
                    run,
                    legacy_resolver=legacy_model_resolver,
                )
            )
            for run in rows
        ]
    finally:
        conn.close()

    traced_statements: list[str] = []

    def traced_connection(board=None, source_errors=None):
        conn = kb.connect(board=board)
        conn.set_trace_callback(traced_statements.append)
        return conn

    monkeypatch.setattr(plugin_module, "_conn", traced_connection)

    # No view query parameter: this exercises the production default detail_view="full".
    response = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert response.status_code == 200, response.text
    actual_runs = response.json()["runs"]

    claimed_event_queries = [
        statement
        for statement in traced_statements
        if "from task_events" in statement.lower()
        and "kind = 'claimed'" in statement.lower()
    ]
    assert len(actual_runs) == len(run_ids) == 53
    assert len(claimed_event_queries) == 1, claimed_event_queries
    assert [_lineage(run) for run in actual_runs] == expected

    drawer = client.get(f"/api/plugins/kanban/tasks/{task_id}?view=drawer")
    assert drawer.status_code == 200, drawer.text
    assert [int(run["id"]) for run in drawer.json()["runs"]] == run_ids[-20:]
