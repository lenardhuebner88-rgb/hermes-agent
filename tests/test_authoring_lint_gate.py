from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm
from tools import kanban_tools


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_AUTHORING_LINT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profiles = ["coordinator", "coder", "reviewer", "research", "dashboard", "default"]
    for profile in profiles:
        target = home if profile == "default" else home / "profiles" / profile
        (target / "skills").mkdir(parents=True, exist_ok=True)
    for profile, skill in [
        ("coordinator", "kanban-orchestrator"),
        ("reviewer", "requesting-code-review"),
        ("research", "avoid-ai-writing"),
        ("coder", "dogfood"),
    ]:
        skill_dir = home / "profiles" / profile / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _task_count() -> int:
    with kb.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _single_task_row() -> dict:
    with kb.connect() as conn:
        row = conn.execute("SELECT id, title, status, assignee FROM tasks").fetchone()
    return dict(row)


def _run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


def test_cli_invalid_template_aborts_before_insert(kanban_home, capsys):
    code = _run_cli(
        [
            "create",
            "bad cli",
            "--assignee",
            "coder",
            "--scope-contract-json",
            json.dumps({"version": 2}),
            "--allowed-tool",
            "not_a_tool",
        ]
    )

    assert code == 2
    assert "authoring lint failed before create" in capsys.readouterr().err
    assert _task_count() == 0


def test_cli_valid_template_creates_normally(kanban_home):
    code = _run_cli(
        [
            "create",
            "good cli",
            "--assignee",
            "coder",
            "--scope-contract-json",
            json.dumps({"version": 2}),
            "--body",
            "Do scoped work.",
            "--json",
        ]
    )

    assert code == 0
    assert _task_count() == 1


def test_cli_raw_assigned_create_routes_to_triage_before_dispatch(kanban_home, capsys):
    code = _run_cli(
        [
            "create",
            "raw cli",
            "--assignee",
            "coder",
            "--body",
            "Do scoped work, but no scope contract yet.",
            "--raw-create",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "raw authoring lint failed; routed task to triage" in captured.err
    data = json.loads(captured.out)
    assert data["status"] == "triage"
    assert data["raw_authoring_lint"]["routed_to_triage"] is True
    assert data["raw_authoring_lint"]["payload"]["ok"] is False
    assert _single_task_row()["status"] == "triage"


def test_cli_raw_create_warn_mode_keeps_ready_for_rollback(kanban_home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AUTHORING_LINT", "warn")
    code = _run_cli(
        [
            "create",
            "raw warn cli",
            "--assignee",
            "coder",
            "--body",
            "No scope contract yet.",
            "--raw-create",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "raw authoring lint warning" in captured.err
    data = json.loads(captured.out)
    assert data["status"] == "ready"
    assert data["raw_authoring_lint"]["routed_to_triage"] is False


def test_cli_unsafe_override_confirms_manual_bypass(kanban_home):
    code = _run_cli(
        [
            "create",
            "unsafe cli",
            "--assignee",
            "coder",
            "--scope-contract-json",
            json.dumps({"version": 2}),
            "--allowed-tool",
            "not_a_tool",
            "--unsafe",
        ]
    )

    assert code == 0
    assert _task_count() == 1


def test_env_warn_mode_is_rollback_flag_and_allows_insert(kanban_home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AUTHORING_LINT", "warn")

    code = _run_cli(
        [
            "create",
            "warn cli",
            "--assignee",
            "coder",
            "--scope-contract-json",
            json.dumps({"version": 2}),
            "--allowed-tool",
            "not_a_tool",
        ]
    )

    assert code == 0
    assert "authoring lint warning" in capsys.readouterr().err
    assert _task_count() == 1


def test_tool_invalid_template_aborts_before_insert(kanban_home):
    out = kanban_tools._handle_create(
        {
            "title": "bad tool",
            "assignee": "coder",
            "scope_contract": {"version": 2},
            "allowed_tools": ["not_a_tool"],
        }
    )

    data = json.loads(out)
    assert "authoring lint failed before create" in data["error"]
    assert _task_count() == 0


def test_tool_valid_template_creates_normally(kanban_home):
    out = kanban_tools._handle_create(
        {
            "title": "good tool",
            "assignee": "coder",
            "body": "Do scoped work.",
            "scope_contract": {"version": 2},
        }
    )

    data = json.loads(out)
    assert data["ok"] is True
    assert _task_count() == 1


def test_tool_raw_assigned_create_routes_to_triage_before_dispatch(kanban_home):
    out = kanban_tools._handle_create(
        {
            "title": "raw tool",
            "assignee": "coder",
            "body": "Do scoped work, but no scope contract yet.",
            "raw_create": True,
        }
    )

    data = json.loads(out)
    assert data["ok"] is True
    assert data["status"] == "triage"
    assert data["raw_authoring_lint"]["routed_to_triage"] is True
    assert _single_task_row()["status"] == "triage"


def test_swarm_invalid_root_template_aborts_before_insert(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="authoring lint failed"):
            create_swarm(
                conn,
                goal="Goal.",
                workers=[SwarmWorkerSpec(profile="coder", title="Worker", body="Body")],
                verifier_assignee="reviewer",
                synthesizer_assignee="research",
                created_by="missing-profile",
            )

    assert _task_count() == 0


def test_swarm_valid_templates_create_root_workers_verifier_synthesizer(kanban_home):
    with kb.connect() as conn:
        created = create_swarm(
            conn,
            goal="Goal.",
            workers=[SwarmWorkerSpec(profile="coder", title="Worker", body="Body")],
            verifier_assignee="reviewer",
            synthesizer_assignee="research",
            created_by="coordinator",
        )

    assert len(created.worker_ids) == 1
    assert _task_count() == 4


def _dashboard_client() -> TestClient:
    from plugins.kanban.dashboard import plugin_api

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_invalid_template_aborts_before_insert(kanban_home):
    client = _dashboard_client()

    response = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "bad dashboard",
            "assignee": "coder",
            "scope_contract": {"version": 2},
            "allowed_tools": ["not_a_tool"],
        },
    )

    assert response.status_code == 400
    assert "authoring lint failed before create" in response.text
    assert _task_count() == 0


def test_dashboard_valid_template_creates_normally(kanban_home):
    client = _dashboard_client()

    response = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "good dashboard",
            "assignee": "coder",
            "body": "Do scoped work.",
            "scope_contract": {"version": 2},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["title"] == "good dashboard"
    assert _task_count() == 1


def test_dashboard_raw_assigned_create_routes_to_triage_before_dispatch(kanban_home):
    client = _dashboard_client()

    response = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "raw dashboard",
            "assignee": "coder",
            "body": "Do scoped work, but no scope contract yet.",
            "raw_create": True,
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["task"]["status"] == "triage"
    assert data["raw_authoring_lint"]["routed_to_triage"] is True
    assert _single_task_row()["status"] == "triage"
