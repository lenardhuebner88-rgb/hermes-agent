import json
from pathlib import Path


def _json_text(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


def test_parse_worktree_porcelain_keeps_identity_without_raw_paths():
    from hermes_cli.operator_inventory import _parse_worktree_porcelain, build_operator_inventory

    raw = """
worktree /home/piet/.hermes/hermes-agent
HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
branch refs/heads/main

worktree /home/piet/.hermes/hermes-agent/.worktrees/codex-feature
HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
branch refs/heads/codex/feature
locked operator hold

worktree /home/piet/.hermes/hermes-agent/.worktrees/kanban/t_123
HEAD cccccccccccccccccccccccccccccccccccccccc
branch refs/heads/kanban/t_123
prunable stale
""".strip()

    parsed = _parse_worktree_porcelain(raw)
    payload = build_operator_inventory(
        {
            "checked_at": 1782070000,
            "worktrees": parsed,
            "actor_groups": [],
            "active_worker_task_ids": [],
            "errors": [],
        },
        repo_root=Path("/home/piet/.hermes/hermes-agent"),
    )

    assert [item["path_label"] for item in payload["worktrees"]] == [
        "main checkout",
        "codex:feature",
        "kanban:t_123",
    ]
    assert payload["worktrees"][1]["locked"] is True
    assert payload["worktrees"][2]["prunable"] is True
    payload_text = _json_text(payload)
    assert "/home/" not in payload_text
    assert ".worktrees/" not in payload_text


def test_build_operator_inventory_derives_real_levers_from_mismatches():
    from hermes_cli.operator_inventory import build_operator_inventory

    payload = build_operator_inventory(
        {
            "checked_at": 1782070000,
            "worktrees": [
                {
                    "path": "/home/piet/.hermes/hermes-agent",
                    "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "branch": "main",
                    "locked": False,
                    "prunable": False,
                    "detached": False,
                    "dirty_count": 0,
                    "untracked_count": 0,
                    "status_checked": True,
                },
                {
                    "path": "/home/piet/.hermes/hermes-agent/.worktrees/kanban/t_123",
                    "head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "branch": "kanban/t_123",
                    "locked": True,
                    "prunable": False,
                    "detached": False,
                    "dirty_count": 3,
                    "untracked_count": 1,
                    "status_checked": True,
                },
                {
                    "path": "/home/piet/.hermes/hermes-agent/.worktrees/codex-feature",
                    "head": "cccccccccccccccccccccccccccccccccccccccc",
                    "branch": "codex/feature",
                    "locked": False,
                    "prunable": False,
                    "detached": False,
                    "dirty_count": None,
                    "untracked_count": None,
                    "status_checked": False,
                },
            ],
            "actor_groups": [
                {
                    "role": "codex",
                    "label": "Codex",
                    "count": 2,
                    "cpu_percent": 12.5,
                    "rss_mb": 512.0,
                    "oldest_age_seconds": 120,
                    "source": "process",
                    "confidence": "medium",
                },
                {
                    "role": "kanban_worker",
                    "label": "Kanban Worker",
                    "count": 1,
                    "cpu_percent": 3.0,
                    "rss_mb": 140.0,
                    "oldest_age_seconds": 500,
                    "source": "canonical",
                    "confidence": "high",
                    "task_ids": ["t_other"],
                    "stale_count": 1,
                },
            ],
            "active_worker_task_ids": ["t_other"],
            "errors": [],
        },
        repo_root=Path("/home/piet/.hermes/hermes-agent"),
    )

    assert payload["schema"] == "hermes-operator-inventory-v1"
    assert payload["summary"] == {
        "worktrees_total": 3,
        "worktrees_locked": 1,
        "worktrees_dirty": 1,
        "worktrees_prunable": 0,
        "worktrees_orphaned": 1,
        "worktrees_status_unknown": 1,
        "actors_total": 3,
        "actors_canonical": 1,
    }
    assert payload["worktrees"][1]["state"] == "dirty"
    assert payload["worktrees"][1]["orphaned"] is True
    assert payload["actors"][0]["role"] == "kanban_worker"
    assert payload["actors"][0]["source"] == "canonical"

    lever_actions = [lever["action"] for lever in payload["levers"]]
    assert lever_actions[:4] == [
        "inspect_dirty_worktrees",
        "inspect_locked_worktrees",
        "inspect_orphan_worktrees",
        "inspect_stale_workers",
    ]
    assert payload["next_lever"]["action"] == "inspect_dirty_worktrees"

    text = _json_text(payload)
    assert "/home/" not in text
    assert ".worktrees/" not in text
    assert "cmdline" not in text.lower()
    assert "sk-" not in text.lower()


def test_operator_inventory_endpoint_returns_snapshot(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient
    from hermes_cli import operator_inventory
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    expected = build = operator_inventory.build_operator_inventory({
        "checked_at": 1782070000,
        "worktrees": [],
        "actor_groups": [],
        "active_worker_task_ids": [],
        "errors": [],
    })
    monkeypatch.setattr(operator_inventory, "snapshot", lambda: expected)

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    response = client.get("/api/operator-inventory")

    assert response.status_code == 200
    assert response.json()["schema"] == "hermes-operator-inventory-v1"
    assert response.json()["summary"] == build["summary"]


def test_collect_kanban_worker_group_reads_existing_db_read_only(monkeypatch, tmp_path):
    import sqlite3

    from hermes_cli import kanban_db, operator_inventory

    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT);
        CREATE TABLE task_runs (
          id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, worker_pid INTEGER,
          started_at INTEGER, last_heartbeat_at INTEGER, ended_at INTEGER, status TEXT, outcome TEXT
        );
        INSERT INTO tasks (id, title, status, assignee) VALUES ('t_live', 'Live worker', 'running', 'coder');
        INSERT INTO task_runs (id, task_id, profile, worker_pid, started_at, last_heartbeat_at, ended_at, status, outcome)
          VALUES (7, 't_live', 'coder', 4242, 1782069900, 1782069990, NULL, 'running', NULL);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.setattr(operator_inventory.time, "time", lambda: 1782070000)

    errors: list[str] = []
    groups, task_ids = operator_inventory._collect_kanban_worker_group(errors)

    assert errors == []
    assert task_ids == {"t_live"}
    assert groups[0]["role"] == "kanban_worker"
    assert groups[0]["source"] == "canonical"
    assert groups[0]["count"] == 1
    assert groups[0]["stale_count"] == 0
