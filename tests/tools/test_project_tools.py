from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from hermes_cli import projects_db
from tools import project_tools


@pytest.fixture(autouse=True)
def _reset_workspace_callback():
    project_tools.set_project_workspace_callback(None)
    yield
    project_tools.set_project_workspace_callback(None)


@pytest.fixture
def project_store(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "projects.db"

    @contextmanager
    def connect_closing():
        conn = projects_db.connect(db_path=db_path)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(projects_db, "connect_closing", connect_closing)
    return db_path


def test_project_create_activates_project_and_moves_bound_workspace(
    project_store: Path,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    calls = []
    project_tools.set_project_workspace_callback(
        lambda task_id, path, name: calls.append((task_id, path, name))
    )

    result = json.loads(
        project_tools.project_create(
            "Hermes Agent",
            str(workspace),
            task_id="task-1",
        )
    )

    assert result["success"] is True
    assert result["slug"] == "hermes-agent"
    assert result["primary_path"] == str(workspace)
    assert calls == [("task-1", str(workspace), "Hermes Agent")]
    with projects_db.connect_closing() as conn:
        assert projects_db.get_active_id(conn) == result["id"]


def test_project_list_and_case_insensitive_switch(
    project_store: Path,
) -> None:
    with projects_db.connect_closing() as conn:
        first = projects_db.create_project(
            conn,
            name="Alpha Workspace",
            folders=["/work/alpha"],
        )
        second = projects_db.create_project(
            conn,
            name="Beta Workspace",
            folders=["/work/beta"],
        )
        projects_db.set_active(conn, first)

    calls = []
    project_tools.set_project_workspace_callback(
        lambda task_id, path, name: calls.append((task_id, path, name))
    )

    listing = json.loads(project_tools.project_list())
    switched = json.loads(
        project_tools.project_switch("BETA-WORKSPACE", task_id="task-2")
    )

    assert listing["active_id"] == first
    assert [item["active"] for item in listing["projects"]] == [True, False]
    assert switched == {
        "success": True,
        "id": second,
        "slug": "beta-workspace",
        "name": "Beta Workspace",
        "primary_path": "/work/beta",
    }
    assert calls == [("task-2", "/work/beta", "Beta Workspace")]


def test_project_tool_validation_returns_structured_errors(
    project_store: Path,
) -> None:
    blank = json.loads(project_tools.project_create("   "))
    missing = json.loads(project_tools.project_switch("missing"))

    assert blank == {"success": False, "error": "name is required"}
    assert missing == {
        "success": False,
        "error": "no project matching 'missing'",
    }
