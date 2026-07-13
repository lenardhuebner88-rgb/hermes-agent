"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest.mock
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


def _load_plugin_module_for_lanes_auth_smoke():
    """Load a fresh plugin module for pure helper tests."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_lanes_auth_smoke_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _review_efficiency_fixture(name: str) -> dict:
    path = (
        Path(__file__).resolve().parents[1]
        / "hermes_cli"
        / "fixtures"
        / "review_efficiency_live_fixtures.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))[name]


def _configure_dashboard_ws(
    monkeypatch, *, token="secret-xyz", bound_host=None, auth_required=False
):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_SESSION_TOKEN", token)
    monkeypatch.setattr(
        web_server.app.state, "auth_required", auth_required, raising=False
    )
    monkeypatch.setattr(web_server.app.state, "bound_host", bound_host, raising=False)
    monkeypatch.setattr(
        web_server.app.state,
        "extra_allowed_hosts",
        frozenset(),
        raising=False,
    )
    return web_server


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    profiles = home / "profiles"
    for name in [
        "default",
        "coder",
        "premium",
        "research",
        "ops",
        "x",
        "a",
        "b",
        "old",
        "new",
        "worker",
        "linguist",
        "alice",
        "bob",
    ]:
        d = profiles / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text("model: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(scope="module")
def kanban_app():
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    app.state.kanban_plugin_module = sys.modules["hermes_dashboard_plugin_kanban_test"]
    return app


@pytest.fixture
def client(kanban_home, kanban_app):
    mod = kanban_app.state.kanban_plugin_module
    sys.modules["hermes_dashboard_plugin_kanban_test"] = mod
    mod._lane_profile_cache = None
    with mod._board_cache_lock:
        mod._board_cache.clear()
    app = kanban_app
    return TestClient(app)


def _push_rows():
    conn = kb.connect()
    try:
        return kb.list_push_subscriptions(conn)
    finally:
        conn.close()


def _install_fake_webpush(monkeypatch, mod, calls, *, status_code=None):
    class FakeWebPushException(Exception):
        def __init__(self, message, response=None):
            super().__init__(message)
            self.response = response

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status

    def fake_webpush(**kwargs):
        calls.append(kwargs)
        if status_code is not None:
            raise FakeWebPushException(
                "push failed", response=FakeResponse(status_code)
            )
        return None

    monkeypatch.setattr(
        mod, "_load_pywebpush", lambda: (fake_webpush, FakeWebPushException)
    )
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-key")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public-key")
    monkeypatch.setenv("VAPID_CLAIMS_SUB", "mailto:test@example.invalid")


def _add_push_sub(endpoint="https://push.example/sub-1"):
    conn = kb.connect()
    try:
        kb.add_push_subscription(
            conn,
            endpoint=endpoint,
            keys_p256dh="p256dh-key",
            keys_auth="auth-key",
        )
    finally:
        conn.close()


def test_push_subscribe_unsubscribe_routes_are_idempotent(kanban_home):
    mod = _load_plugin_module_for_lanes_auth_smoke()
    body = {
        "endpoint": "https://push.example/sub-1",
        "keys": {"p256dh": "p256dh-a", "auth": "auth-a"},
    }
    response = mod.subscribe_push(mod.PushSubscriptionBody(**body), board=None)

    assert response == {"ok": True}
    rows = _push_rows()
    assert len(rows) == 1
    assert rows[0]["endpoint"] == body["endpoint"]

    body["keys"] = {"p256dh": "p256dh-b", "auth": "auth-b"}
    response = mod.subscribe_push(mod.PushSubscriptionBody(**body), board=None)

    assert response == {"ok": True}
    rows = _push_rows()
    assert len(rows) == 1
    assert rows[0]["keys_p256dh"] == "p256dh-b"
    assert rows[0]["fail_count"] == 0

    with pytest.raises(Exception):
        mod.PushSubscriptionBody(
            endpoint="https://push.example/bad",
            keys={"p256dh": "missing-auth"},
        )
    assert len(_push_rows()) == 1

    response = mod.unsubscribe_push(
        mod.PushUnsubscribeBody(endpoint="https://push.example/sub-1"),
        board=None,
    )

    assert response["removed"] is True
    assert _push_rows() == []


def test_push_send_path_sends_once_and_deletes_expired_subscription(
    kanban_home, monkeypatch
):
    mod = _load_plugin_module_for_lanes_auth_smoke()
    mod._PUSH_DISABLED_REASONS_LOGGED.clear()
    _add_push_sub("https://push.example/sub-1")
    calls = []
    _install_fake_webpush(monkeypatch, mod, calls)

    result = mod._send_web_push_payload(
        board=None,
        payload={
            "title": "Entscheidung nötig",
            "body": "operator approval",
            "tag": "t",
            "task_id": "t_push",
            "url": "/control/flow?task=t_push",
        },
    )

    assert result == {"enabled": True, "sent": 1, "removed": 0, "failed": 0}
    assert len(calls) == 1
    assert _push_rows()[0]["last_success_at"] is not None

    calls.clear()
    _install_fake_webpush(monkeypatch, mod, calls, status_code=410)
    result = mod._send_web_push_payload(
        board=None,
        payload={
            "title": "Entscheidung nötig",
            "body": "operator approval",
            "tag": "t",
            "task_id": "t_push",
            "url": "/control/flow?task=t_push",
        },
    )

    assert result == {"enabled": True, "sent": 0, "removed": 1, "failed": 0}
    assert len(calls) == 1
    assert _push_rows() == []


def test_push_missing_vapid_env_disables_without_crash(kanban_home, monkeypatch):
    mod = _load_plugin_module_for_lanes_auth_smoke()
    mod._PUSH_DISABLED_REASONS_LOGGED.clear()
    _add_push_sub()
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_CLAIMS_SUB", raising=False)
    monkeypatch.setattr(
        mod,
        "_load_pywebpush",
        lambda: pytest.fail("pywebpush must not load when VAPID env is missing"),
    )

    result = mod._send_web_push_payload(
        board=None,
        payload={
            "title": "Entscheidung nötig",
            "body": "operator approval",
            "tag": "t",
            "task_id": "t_push",
            "url": "/control/flow?task=t_push",
        },
    )

    assert result == {"enabled": False, "sent": 0, "removed": 0, "failed": 0}
    assert len(_push_rows()) == 1


def test_push_hook_filters_only_operator_blocks_and_chain_roots(
    kanban_home, monkeypatch
):
    mod = _load_plugin_module_for_lanes_auth_smoke()
    mod._PUSH_DISABLED_REASONS_LOGGED.clear()
    _add_push_sub()
    calls = []
    _install_fake_webpush(monkeypatch, mod, calls)
    conn = kb.connect()
    try:
        worker = kb.create_task(conn, title="Worker node", assignee="worker")
        root = kb.create_task(conn, title="Root sink", assignee="worker")
        kb.link_tasks(conn, worker, root)
    finally:
        conn.close()

    mod._handle_blocked_push(
        task_id=worker,
        board=None,
        reason="dependency failed",
    )
    assert calls == []

    mod._handle_blocked_push(
        task_id=worker,
        board=None,
        reason="operator approval needed",
    )
    assert len(calls) == 1

    calls.clear()
    mod._handle_completed_push(task_id=worker, board=None, summary="worker done")
    assert calls == []

    mod._handle_completed_push(task_id=root, board=None, summary="chain done")
    assert len(calls) == 1
    payload = json.loads(calls[0]["data"])
    assert payload["title"] == "Kette fertig"
    assert payload["task_id"] == root


def test_planspecs_endpoint_passes_valid_and_limit(monkeypatch, client):
    from hermes_cli import planspecs
    from hermes_constants import get_hermes_home  # noqa: WPS433 (intentional)

    calls = []

    def fake_list_planspecs(**kwargs):
        calls.append(kwargs)
        return [{"path": "/tmp/one.md", "valid": True, "open": True, "errors": []}]

    monkeypatch.setattr(planspecs, "list_planspecs", fake_list_planspecs)

    response = client.get("/api/plugins/kanban/planspecs?scope=open&valid=true&limit=8")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert calls == [
        {
            "scope": "open",
            "valid": True,
            "limit": 8,
            "search": None,
            "include_kanban_status": True,
            "board": None,
            "prose_plans_root": get_hermes_home() / "dashboard" / "prose-plans",
        }
    ]


def test_planspecs_endpoint_surfaces_ingest_precheck_fields(monkeypatch, client):
    """The /planspecs endpoint must pass through ingest_disposition,
    ingest_would_block, and ingest_findings so the dashboard can show
    inline ingest blockers before the operator clicks Kanban."""
    from hermes_cli import planspecs

    def fake_list_planspecs(**kwargs):
        return [
            {
                "path": "/tmp/binding.md",
                "valid": True,
                "open": True,
                "binding": True,
                "ingest_disposition": "invalid",
                "ingest_would_block": True,
                "ingest_findings": ["placeholder residue in section B1-S2"],
                "errors": [],
            }
        ]

    monkeypatch.setattr(planspecs, "list_planspecs", fake_list_planspecs)

    response = client.get("/api/plugins/kanban/planspecs?scope=all")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    rec = body["planspecs"][0]
    assert rec["ingest_disposition"] == "invalid"
    assert rec["ingest_would_block"] is True
    assert rec["ingest_findings"] == ["placeholder residue in section B1-S2"]


# ---------------------------------------------------------------------------
# POST /lanes/auth-smoke pure helpers
# ---------------------------------------------------------------------------


def test_lanes_auth_smoke_parser_scopes_provider_model_to_session_and_no_fallback():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()
    lines = [
        "2026-06-21 INFO [older] agent.conversation_loop: API call #1: model=wrong provider=wrong",
        "2026-06-21 INFO [session_abc] agent.turn_context: conversation turn: session=session_abc model=z-ai/glm-5.2 provider=openrouter",
        "2026-06-21 INFO [session_abc] agent.conversation_loop: API call #1: model=z-ai/glm-5.2 provider=openrouter",
        "2026-06-21 INFO [session_abc] agent.conversation_loop: Turn ended: reason=text_response model=z-ai/glm-5.2 session=session_abc",
    ]

    result = plugin_api._parse_lanes_auth_smoke_log(lines, session_id="session_abc")

    assert result["observed_provider"] == "openrouter"
    assert result["observed_model"] == "z-ai/glm-5.2"
    assert result["fallback_activated"] is False
    assert result["error_class"] is None
    assert result["session_id"] == "session_abc"


def test_lanes_auth_smoke_parser_detects_fallback_and_errors():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()

    fallback = plugin_api._parse_lanes_auth_smoke_log([
        "provider=openrouter model=bad/model",
        "Fallback activated: provider=openai-codex model=gpt-5.5",
        "provider=openai-codex model=gpt-5.5 response complete",
    ])
    auth = plugin_api._parse_lanes_auth_smoke_log(["401 Missing bearer/basic"])
    quota = plugin_api._parse_lanes_auth_smoke_log(["429 RESOURCE_EXHAUSTED quota"])

    assert fallback["observed_provider"] == "openai-codex"
    assert fallback["observed_model"] == "gpt-5.5"
    assert fallback["fallback_activated"] is True
    assert auth["error_class"] == "auth_error"
    assert quota["error_class"] == "quota_or_rate_limit"


def test_lanes_auth_smoke_command_is_bounded_and_profile_scoped():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()

    command = plugin_api._build_lanes_auth_smoke_command(
        python_bin="/home/piet/.hermes/hermes-agent/venv/bin/python",
        profile="reviewer",
        provider="openrouter",
        model="z-ai/glm-5.2",
        token="lanes-auth-smoke-reviewer-abc123",
    )

    assert command[:5] == [
        "/home/piet/.hermes/hermes-agent/venv/bin/python",
        "-m",
        "hermes_cli.main",
        "--profile",
        "reviewer",
    ]
    assert "chat" in command
    assert "--max-turns" in command
    assert "1" in command
    assert "--ignore-rules" in command
    assert "--source" in command
    assert "lanes-auth-smoke" in command
    assert "--provider" in command
    assert "openrouter" in command
    assert "--model" in command
    assert "z-ai/glm-5.2" in command


def test_lanes_auth_smoke_redacts_sensitive_text():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()
    text = "401 OPENROUTER_API_KEY=sk-secret bearer abc token xyz"

    redacted = plugin_api._redact_lanes_auth_smoke_text(text)

    assert "sk-secret" not in redacted
    assert "bearer abc" not in redacted.lower()
    assert "token xyz" not in redacted.lower()


def test_lanes_auth_smoke_status_and_reasoning_are_honest():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()

    ok = plugin_api._derive_lanes_auth_smoke_status(
        returncode=0,
        response_exact=True,
        requested_provider="openrouter",
        requested_model="z-ai/glm-5.2",
        observed_provider="openrouter",
        observed_model="z-ai/glm-5.2",
        fallback_activated=False,
        error_class=None,
    )
    fallback = plugin_api._derive_lanes_auth_smoke_status(
        returncode=0,
        response_exact=True,
        requested_provider="openrouter",
        requested_model="bad/model",
        observed_provider="openai-codex",
        observed_model="gpt-5.5",
        fallback_activated=True,
        error_class=None,
    )
    reason = plugin_api._explain_lanes_auth_smoke_result(
        status="fallback",
        requested_provider="openrouter",
        requested_model="bad/model",
        observed_provider="openai-codex",
        observed_model="gpt-5.5",
        response_exact=True,
        fallback_activated=True,
        error_class=None,
    )

    assert ok == "ok"
    assert fallback == "fallback"
    assert "requested openrouter/bad/model" in reason
    assert "observed openai-codex/gpt-5.5" in reason
    assert "fallback activated" in reason


def test_lanes_auth_smoke_selects_effective_catalog_roles_and_skips_claude_runtime():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()
    lane = {
        "id": "lane_1",
        "profiles": {
            "reviewer": {
                "worker_runtime": "hermes",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
            },
            "premium": {"worker_runtime": "claude-cli", "model": "claude-opus-4-8"},
        },
    }
    catalog = [
        {
            "name": "coder",
            "worker_runtime": "hermes",
            "default_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        },
        {
            "name": "reviewer",
            "worker_runtime": "hermes",
            "default_provider": "openai-codex",
            "default_model": "gpt-5.5",
        },
        {
            "name": "premium",
            "worker_runtime": "claude-cli",
            "default_model": "claude-opus-4-8",
        },
    ]

    results = plugin_api._select_lanes_auth_smoke_roles(lane, [], catalog)

    assert [row["role"] for row in results] == ["coder", "reviewer", "premium"]
    assert results[0]["runtime"] == "hermes"
    assert results[0]["provider"] == "openai-codex"
    assert results[0]["model"] == "gpt-5.4-mini"
    assert results[1]["provider"] == "openrouter"
    assert results[1]["model"] == "z-ai/glm-5.2"
    assert results[2]["runtime"] == "claude-cli"


def test_lanes_auth_smoke_builds_blocking_summary_and_scope_counts():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()
    results = [
        {"role": "coder", "status": "ok", "fallback_activated": False},
        {
            "role": "research",
            "status": "quota_or_rate_limit",
            "fallback_activated": True,
        },
        {"role": "premium", "status": "skipped", "fallback_activated": False},
    ]

    summary = plugin_api._summarize_lanes_auth_smoke(
        results,
        total_role_count=5,
        checked_role_count=3,
        truncated=False,
    )

    assert summary["safe_to_activate"] is False
    assert summary["decision"] == "blocked"
    assert summary["blocking_roles"] == ["research"]
    assert summary["fallback_roles"] == ["research"]
    assert summary["skipped_roles"] == ["premium"]
    assert summary["checked_role_count"] == 3
    assert summary["total_role_count"] == 5
    assert "Research" in summary["recommended_next_action"]


def test_lanes_auth_smoke_pure_fallback_is_restricted_not_blocked():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()

    summary = plugin_api._summarize_lanes_auth_smoke(
        [{"role": "coder", "status": "fallback", "fallback_activated": True}],
        total_role_count=1,
        checked_role_count=1,
        truncated=False,
    )

    assert summary["safe_to_activate"] is False
    assert summary["decision"] == "restricted"
    assert summary["blocking_roles"] == []
    assert summary["fallback_roles"] == ["coder"]


def test_lanes_auth_smoke_empty_summary_is_not_ok():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()

    summary = plugin_api._summarize_lanes_auth_smoke(
        [],
        total_role_count=0,
        checked_role_count=0,
        truncated=False,
    )

    assert summary["safe_to_activate"] is False
    assert summary["decision"] == "blocked"
    assert summary["blocking_roles"] == []
    assert "Keine Rollen" in summary["recommended_next_action"]


def test_lanes_auth_smoke_limits_roles_server_side():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()
    lane = {"id": "lane_1", "profiles": {}}
    catalog = [
        {
            "name": f"role{i}",
            "worker_runtime": "hermes",
            "default_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        }
        for i in range(plugin_api.LANES_AUTH_SMOKE_ROLE_LIMIT + 3)
    ]

    roles = plugin_api._select_lanes_auth_smoke_roles(lane, [], catalog)
    truncated = len(roles) > plugin_api.LANES_AUTH_SMOKE_ROLE_LIMIT

    assert len(roles) == plugin_api.LANES_AUTH_SMOKE_ROLE_LIMIT + 3
    assert truncated is True


def test_lanes_auth_smoke_redacts_common_secret_shapes():
    plugin_api = _load_plugin_module_for_lanes_auth_smoke()
    text = (
        "Authorization: Basic abc123 "
        "Authorization: Bearer sk-proj-123 "
        '"api_key": "sk-secret-json" '
        "OPENROUTER_API_KEY=sk-secret-env "
        "token: ghp_secret "
        "GITHUB_TOKEN=GHP-UPPER "
        "standalone ghp-secret"
    )

    redacted = plugin_api._redact_lanes_auth_smoke_text(text)

    assert "abc123" not in redacted
    assert "sk-proj-123" not in redacted
    assert "sk-secret-json" not in redacted
    assert "sk-secret-env" not in redacted
    assert "ghp_secret" not in redacted
    assert "GHP-UPPER" not in redacted
    assert "ghp-secret" not in redacted


# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------


def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == kb.VALID_STATUSES - {"archived"}
    for expected in (
        "triage",
        "todo",
        "scheduled",
        "ready",
        "running",
        "blocked",
        "done",
    ):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == []
    assert data["latest_event_id"] == 0


def test_archive_board_is_cursor_paginated_searchable_and_separate_from_active_poll(client):
    conn = kb.connect()
    try:
        archived_ids = []
        for index, title in enumerate(
            ["old alpha", "old beta", "needle archive", "old delta", "old epsilon"],
            start=1,
        ):
            task_id = kb.create_task(conn, title=title, assignee="alice" if index % 2 else "bob")
            assert kb.archive_task(conn, task_id) is True
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'archived'",
                (1_780_000_000 + index, task_id),
            )
            archived_ids.append(task_id)
    finally:
        conn.close()

    active = client.get(
        "/api/plugins/kanban/board?card_diagnostics=summary&card_body=none"
    ).json()
    assert all(
        task["status"] != "archived"
        for column in active["columns"]
        for task in column["tasks"]
    )
    assert "archive" not in active

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/api/plugins/kanban/board/archive", params=params)
        assert response.status_code == 200
        page = response.json()
        assert page["total_count"] == 5
        assert page["filtered_count"] == 5
        assert page["loaded_count"] == len(page["tasks"])
        assert page["limit"] == 2
        assert all(task["status"] == "archived" for task in page["tasks"])
        assert all(task["archived_at"] > 0 for task in page["tasks"])
        seen.extend(task["id"] for task in page["tasks"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
        assert cursor

    assert seen == list(reversed(archived_ids))
    assert len(seen) == len(set(seen)) == 5

    filtered = client.get(
        "/api/plugins/kanban/board/archive",
        params={"q": "needle", "assignee": "alice", "limit": 50},
    ).json()
    assert filtered["total_count"] == 5
    assert filtered["filtered_count"] == 1
    assert filtered["loaded_count"] == 1
    assert filtered["tasks"][0]["title"] == "needle archive"
    assert filtered["query"] == "needle"
    assert filtered["assignee"] == "alice"

    invalid = client.get(
        "/api/plugins/kanban/board/archive", params={"cursor": "not-a-cursor"}
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid archive cursor"


# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------


def test_dashboard_create_normalizes_legacy_assignee_alias(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "legacy alias", "assignee": "coder-claude"},
    )

    assert r.status_code == 200
    assert r.json()["task"]["assignee"] == "premium"


def test_dashboard_patch_normalizes_legacy_assignee_alias_and_allows_unassign(client):
    created = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "alias patch", "assignee": "coder"},
    ).json()["task"]

    patched = client.patch(
        f"/api/plugins/kanban/tasks/{created['id']}",
        json={"assignee": "coder-claude"},
    )
    assert patched.status_code == 200
    assert patched.json()["task"]["assignee"] == "premium"

    cleared = client.patch(
        f"/api/plugins/kanban/tasks/{created['id']}",
        json={"assignee": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["task"]["assignee"] is None


def test_create_task_rejects_non_spawnable_assignee(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "bad lane", "assignee": "no-such-profile"},
    )

    assert r.status_code == 400
    assert "not spawnable" in r.json()["detail"]


def test_patch_task_preserves_unchanged_off_disk_assignee_for_repair(client):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy active", assignee="ghost_lane")
    finally:
        conn.close()

    edited = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"title": "legacy active edited", "assignee": "ghost_lane"},
    )
    assert edited.status_code == 200
    assert edited.json()["task"]["title"] == "legacy active edited"
    assert edited.json()["task"]["assignee"] == "ghost_lane"

    repaired = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"assignee": "coder"},
    )
    assert repaired.status_code == 200
    assert repaired.json()["task"]["assignee"] == "coder"

    rejected = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"assignee": "other_ghost"},
    )
    assert rejected.status_code == 400


def test_researcher_is_not_spawnable_when_not_on_disk(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "bad researcher", "assignee": "researcher"},
    )

    assert r.status_code == 400
    assert "researcher" in r.json()["detail"]


def test_patch_task_rejects_non_spawnable_assignee(client):
    created = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "good lane", "assignee": "coder"},
    ).json()["task"]

    r = client.patch(
        f"/api/plugins/kanban/tasks/{created['id']}",
        json={"assignee": "no-such-profile"},
    )

    assert r.status_code == 400
    assert "not spawnable" in r.json()["detail"]


def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "research",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "research"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "research" in data["assignees"]


def test_board_surfaces_active_review_stage_only_while_in_review(client):
    """Slice b: a task in ``review`` carries ``active_review_stage`` (the latest
    submitted_for_review target_profile) on its board card; a task NOT in review
    never shows the field (no stale stage)."""
    with kb.connect() as conn:
        reviewing = kb.create_task(conn, title="in review", assignee="coder")
        done_once = kb.create_task(
            conn, title="was reviewed, now done", assignee="coder"
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (reviewing,))
            kb._append_event(
                conn, reviewing, "submitted_for_review", {"target_profile": "reviewer"}
            )
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (done_once,))
            kb._append_event(
                conn, done_once, "submitted_for_review", {"target_profile": "verifier"}
            )

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    cols = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    review_card = next(t for t in cols["review"] if t["id"] == reviewing)
    assert review_card["active_review_stage"] == "reviewer"
    done_card = next(t for t in cols["done"] if t["id"] == done_once)
    assert (
        done_card.get("active_review_stage") is None
    )  # not in review → no stale stage


def test_board_retries_transient_corrupt_open_and_keeps_flow_card_visible(
    client, monkeypatch
):
    """A transient corrupt-open signal must not make the Flow board vanish.

    Runtime evidence for the Discord-vorfilter incident showed a dashboard
    ``GET /board`` request failing at the DB-open boundary with
    ``KanbanDbCorruptError`` while a fresh integrity probe immediately after was
    healthy.  The dashboard should retry that one transient open and still return
    the newly captured flow-capture triage card to the UI.
    """
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="discord-vorfilter-fixture",
            created_by="discord-idee",
            tenant="flow-capture",
            triage=True,
        )
    finally:
        conn.close()

    real_connect = kb.connect
    calls = {"count": 0}

    def flaky_connect(*args, **kwargs):
        if calls["count"] == 0:
            calls["count"] += 1
            raise kb.KanbanDbCorruptError(
                kb.kanban_db_path(),
                None,
                "transient integrity_check returned malformed",
            )
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kb, "connect", flaky_connect)

    r = client.get("/api/plugins/kanban/board?card_diagnostics=summary&card_body=none")

    assert r.status_code == 200, r.text
    data = r.json()
    assert calls["count"] == 1
    source_errors = data.get("source_errors")
    assert source_errors and len(source_errors) == 1
    assert source_errors[0]["artifact"] == "kanban_board_fetch"
    assert source_errors[0]["stage"] == "db_open"
    assert source_errors[0]["source"] == "kanban_db"
    assert source_errors[0]["severity"] == "warning"
    assert source_errors[0]["retry_count"] == 1
    assert "transient integrity_check" in source_errors[0]["message"]
    tasks = [task for column in data["columns"] for task in column["tasks"]]
    card = next(task for task in tasks if task["id"] == task_id)
    assert card["title"] == "discord-vorfilter-fixture"
    assert card["tenant"] == "flow-capture"
    assert card["status"] == "triage"


def test_flow_lean_auto_capture_stays_triage_without_planspec(
    client, tmp_path, monkeypatch
):
    """Characterize the no-PlanSpec Flow path used by lean+AUTO capture.

    The UI routes lean+AUTO through the plain ``POST /tasks`` endpoint, not
    ``/tasks/flow-capture``. That keeps the task in triage for the gateway
    decomposer and must not create a durable flow plan/spec marker.
    """
    spec_dir = tmp_path / "flow-plans"
    monkeypatch.setenv("HERMES_FLOW_PLANS_DIR", str(spec_dir))

    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Lean auto no planspec",
            "assignee": None,
            "priority": 0,
            "tenant": "flow-capture",
            "triage": True,
            "park": False,
            "notify_home": False,
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    task_id = task["id"]
    assert task["status"] == "triage"
    assert task["assignee"] is None
    assert task["tenant"] == "flow-capture"

    r = client.get("/api/plugins/kanban/board?card_diagnostics=summary&card_body=none")
    assert r.status_code == 200, r.text
    tasks = [item for column in r.json()["columns"] for item in column["tasks"]]
    card = next(item for item in tasks if item["id"] == task_id)
    assert card["status"] == "triage"
    assert card["assignee"] is None
    assert card["tenant"] == "flow-capture"

    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/flow-plan")
    assert r.status_code == 404
    assert not (spec_dir / f"{task_id}.md").exists()
    with kb.connect() as conn:
        persisted = kb.get_task(conn, task_id)
        events = [event.kind for event in kb.list_events(conn, task_id)]
    assert persisted is not None
    assert persisted.status == "triage"
    assert persisted.assignee is None
    assert "flow_plan" not in events
    assert "specified" not in events


def test_board_card_diagnostics_summary_omits_detail(client, monkeypatch):
    """``card_diagnostics=summary`` drops the per-card structured diagnostics
    list (the bulk of the /control poll payload) but keeps the compact
    ``warnings`` badge; the default (``full``) still embeds the list for the
    kanban dashboard drawer."""
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Diagnose me"},
    ).json()["task"]["id"]

    # The rule engine needs a specific event history to fire; here we only test
    # the payload-shaping branch, so stub a deterministic diagnostic.
    mod = sys.modules["hermes_dashboard_plugin_kanban_test"]
    fake = {task_id: [{"kind": "test_diag", "severity": "warning", "count": 1}]}
    monkeypatch.setattr(mod, "_compute_task_diagnostics", lambda *a, **k: fake)

    def _card(url):
        data = client.get(url).json()
        for col in data["columns"]:
            for c in col["tasks"]:
                if c["id"] == task_id:
                    return c
        raise AssertionError(f"card {task_id} missing from {url}")

    full = _card("/api/plugins/kanban/board")
    assert full.get("diagnostics") == fake[task_id]
    assert full.get("warnings", {}).get("count") == 1

    summary = _card("/api/plugins/kanban/board?card_diagnostics=summary")
    assert "diagnostics" not in summary  # detail dropped …
    assert summary.get("warnings", {}).get("count") == 1  # … badge kept


def test_board_card_body_none_omits_long_text(client):
    """``card_body=none`` drops body+result per card (the /control poller's
    schema strips both anyway — body dominates real-board payloads); the
    default (``full``) keeps them for the kanban dashboard."""
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Trim me", "body": "a long body " * 50},
    ).json()["task"]["id"]

    def _card(url):
        data = client.get(url).json()
        for col in data["columns"]:
            for c in col["tasks"]:
                if c["id"] == task_id:
                    return c
        raise AssertionError(f"card {task_id} missing from {url}")

    full = _card("/api/plugins/kanban/board")
    assert full.get("body", "").startswith("a long body")
    assert "result" in full

    trimmed = _card("/api/plugins/kanban/board?card_body=none")
    assert "body" not in trimmed
    assert "result" not in trimmed
    # Card identity + the fields the /control board renders survive.
    assert trimmed["title"] == "Trim me"
    assert "latest_summary" in trimmed
    assert "root_id" in trimmed


def test_board_card_body_none_preserves_review_tier(client):
    """Phase C: the Flow-Tab Stage-Pill reads review_tier from the /board poll —
    card_body=none must NOT strip it (it only drops body+result)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="tiered", assignee="coder", review_tier="critical"
        )
    data = client.get(
        "/api/plugins/kanban/board?card_diagnostics=summary&card_body=none"
    ).json()
    card = next(
        (t for col in data["columns"] for t in col["tasks"] if t["id"] == tid), None
    )
    assert card is not None, "tiered task missing from board"
    assert card["review_tier"] == "critical"


def test_board_etag_304_roundtrip(client):
    """The board sends a weak ETag (computed without the per-second ``now``)
    and answers an If-None-Match revalidation with 304 while the board is
    unchanged; any mutation changes the ETag again."""
    client.post("/api/plugins/kanban/tasks", json={"title": "etag probe"})

    r1 = client.get("/api/plugins/kanban/board")
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag and etag.startswith('W/"')
    assert r1.headers.get("cache-control") == "private, no-cache"
    assert "now" in r1.json()

    # Unchanged board → 304, no body — even when the wall clock moved on
    # (per-card "age" is derived from hashed timestamps and excluded from
    # the ETag basis; otherwise the tag would change every second).
    r2 = client.get(
        "/api/plugins/kanban/board",
        headers={"If-None-Match": etag},
    )
    assert r2.status_code == 304
    assert r2.headers.get("etag") == etag

    with unittest.mock.patch.object(
        kb,
        "task_age",
        return_value={
            "created_age_seconds": 99999,
            "started_age_seconds": None,
            "time_to_complete_seconds": None,
        },
    ):
        r2b = client.get(
            "/api/plugins/kanban/board",
            headers={"If-None-Match": etag},
        )
    assert r2b.status_code == 304

    # A mutation (new task) invalidates the tag.
    client.post("/api/plugins/kanban/tasks", json={"title": "etag buster"})
    r3 = client.get(
        "/api/plugins/kanban/board",
        headers={"If-None-Match": etag},
    )
    assert r3.status_code == 200
    assert r3.headers.get("etag") != etag


def test_board_payload_cache_reuses_unchanged_payload(client, monkeypatch):
    """A second poll with identical params and an unchanged DB is served from
    the server-side payload cache (no diagnostics recompute, identical body
    apart from ``now``), and a board-visible mutation invalidates it."""
    client.post("/api/plugins/kanban/tasks", json={"title": "cache me"})

    mod = sys.modules["hermes_dashboard_plugin_kanban_test"]
    real = mod._compute_task_diagnostics
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(mod, "_compute_task_diagnostics", counting)

    url = "/api/plugins/kanban/board?card_diagnostics=summary&card_body=none"
    r1 = client.get(url)
    assert r1.status_code == 200
    assert calls["n"] == 1

    r2 = client.get(url)
    assert r2.status_code == 200
    assert calls["n"] == 1  # cache hit — payload not rebuilt
    assert r2.headers.get("etag") == r1.headers.get("etag")

    d1, d2 = r1.json(), r2.json()
    assert d1.pop("now") and d2.pop("now")
    assert d1 == d2

    # ETag revalidation rides the cache too: 304 without recompute.
    r3 = client.get(url, headers={"If-None-Match": r2.headers["ETag"]})
    assert r3.status_code == 304
    assert calls["n"] == 1

    # A board-visible mutation bumps the DB stamp → fresh payload next poll.
    new_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "invalidate cache"},
    ).json()["task"]["id"]
    r4 = client.get(url)
    assert r4.status_code == 200
    assert calls["n"] == 2
    ids = [t["id"] for col in r4.json()["columns"] for t in col["tasks"]]
    assert new_id in ids


def test_board_payload_cache_ttl_expiry_and_disable(client, monkeypatch):
    """Time-driven diagnostics rules flip without any DB write, so a stamp
    match alone must never serve an entry past the max TTL; TTL 0 disables
    the cache entirely (ops escape hatch)."""
    client.post("/api/plugins/kanban/tasks", json={"title": "ttl probe"})

    mod = sys.modules["hermes_dashboard_plugin_kanban_test"]
    real = mod._compute_task_diagnostics
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(mod, "_compute_task_diagnostics", counting)

    monkeypatch.setattr(mod, "_BOARD_CACHE_TTL_S", 0.05)
    assert client.get("/api/plugins/kanban/board").status_code == 200
    time.sleep(0.06)
    assert client.get("/api/plugins/kanban/board").status_code == 200
    assert calls["n"] == 2  # TTL elapsed → recompute despite unchanged stamp

    monkeypatch.setattr(mod, "_BOARD_CACHE_TTL_S", 0.0)
    assert client.get("/api/plugins/kanban/board").status_code == 200
    assert client.get("/api/plugins/kanban/board").status_code == 200
    assert calls["n"] == 4  # cache disabled → every poll recomputes


def test_create_task_park_lands_in_scheduled(client):
    # The dashboard "copy to Fleet" action sends triage=True + park=True so the
    # new task is parked in `scheduled` (Plan stage) instead of being
    # auto-specified/decomposed (triage) or auto-dispatched (ready). The
    # operator clicks Dispatch in the Fleet to launch it.
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "[FO] Essensplan-Zutaten",
            "assignee": "coder",
            "tenant": "family-organizer",
            "triage": True,
            "park": True,
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["status"] == "scheduled", task
    assert task["assignee"] == "coder"
    task_id = task["id"]

    # Board lists it under `scheduled`, not triage/ready.
    data = client.get("/api/plugins/kanban/board").json()
    scheduled = next(c for c in data["columns"] if c["name"] == "scheduled")
    assert any(t["id"] == task_id for t in scheduled["tasks"]), data
    for col_name in ("triage", "ready", "running"):
        col = next(c for c in data["columns"] if c["name"] == col_name)
        assert all(t["id"] != task_id for t in col["tasks"])


def test_commission_idempotency_returns_existing_card(client):
    # Backlog -> Kanban: a second click with the same idempotency_key must
    # return the EXISTING card, not create a duplicate (FO/Orchestrator
    # "create real Kanban card" dedup).
    payload = {
        "title": "[FO] Essensplan-Zutaten",
        "assignee": "coder",
        "tenant": "family-organizer",
        "triage": True,
        "park": True,
        "idempotency_key": "fo-backlog:0126",
    }
    first = client.post("/api/plugins/kanban/tasks", json=payload)
    assert first.status_code == 200, first.text
    first_id = first.json()["task"]["id"]

    second = client.post("/api/plugins/kanban/tasks", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["task"]["id"] == first_id  # same card, no duplicate

    # Board has exactly one task for this idempotency key.
    data = client.get("/api/plugins/kanban/board").json()
    all_ids = [t["id"] for col in data["columns"] for t in col["tasks"]]
    assert all_ids.count(first_id) == 1


def test_create_task_without_park_keeps_triage(client):
    # Sanity: without park, triage=True still lands in triage (unchanged path).
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "raw", "assignee": "coder", "triage": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "triage"


def test_create_task_title_over_cap_returns_422(client):
    r = client.post("/api/plugins/kanban/tasks", json={"title": "x" * 513})
    assert r.status_code == 422


def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])


def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1


def test_board_query_param_default_overrides_current_board_pointer(client):
    """Dashboard ``?board=default`` must win even if the CLI's current-board
    pointer targets a non-default board.

    Regression: selecting the Default board in the dashboard must not fall
    through to whichever board ``hermes kanban boards switch`` last pinned.
    """
    default_task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "default-only"},
    ).json()["task"]

    kb.create_board("other")
    other_conn = kb.connect(board="other")
    try:
        kb.create_task(other_conn, title="other-only")
    finally:
        other_conn.close()

    kb.set_current_board("other")

    current_board = client.get("/api/plugins/kanban/board").json()
    current_ids = {
        task["id"] for column in current_board["columns"] for task in column["tasks"]
    }
    assert default_task["id"] not in current_ids

    pinned_default = client.get("/api/plugins/kanban/board?board=default").json()
    pinned_ids = {
        task["id"] for column in pinned_default["columns"] for task in column["tasks"]
    }
    assert pinned_ids == {default_task["id"]}


def test_dashboard_select_filters_use_sdk_value_change_handler():
    """Tenant/assignee filters must work with the dashboard SDK Select API.

    The dashboard Select component is shadcn-like and calls
    ``onValueChange(value)`` instead of native ``onChange(event)``. A native-only
    handler leaves the tenant dropdown visually selectable but never updates the
    filtered board query.
    """

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "function selectChangeHandler(setter)" in js
    assert "onValueChange: function (v)" in js
    assert "onChange: function (e)" in js
    assert "selectChangeHandler(props.setTenantFilter)" in js
    assert "selectChangeHandler(props.setAssigneeFilter)" in js


def test_dashboard_client_side_filtering_includes_tenant_filter():
    """The rendered board must also filter by tenant.

    The API request includes ``?tenant=...``, but the dashboard also filters the
    locally cached board for search/assignee changes. Without checking
    ``tenantFilter`` here, switching tenants can leave stale cards visible until a
    full reload finishes.
    """

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "if (tenantFilter && t.tenant !== tenantFilter) return false;" in js
    assert "[boardData, tenantFilter, assigneeFilter, search]" in js


def test_dashboard_initial_board_uses_backend_current_when_unpinned():
    """Fresh browsers should open the backend current board, not default.

    Explicit dashboard selections are stored in localStorage and should still
    win, but an empty localStorage state must adopt the API's ``current`` board
    so multi-board installs do not look empty on first load.
    """

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "useState(() => readSelectedBoard() || null)" in js
    assert "const storedBoard = readSelectedBoard();" in js
    assert "if (!storedBoard && !board && data && data.current)" in js
    assert "setBoard(data.current);" in js
    assert 'readSelectedBoard() || "default"' not in js


def test_dashboard_markdown_html_is_sanitized_before_render():
    """Markdown rendering must sanitize HTML before dangerouslySetInnerHTML."""

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "function sanitizeMarkdownHtml(html)" in js
    assert "MARKDOWN_ALLOWED_TAGS" in js
    assert 'sanitizeMarkdownHtml(renderMarkdown(props.source || ""))' in js
    assert (
        'dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || "") }'
        not in js
    )


# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------


def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]
    assert data["links"]["parent_states"] == [
        {"id": parent["id"], "title": "parent", "status": parent["status"]}
    ]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]
    assert r.json()["links"]["child_states"] == [
        {"id": child["id"], "title": "child", "status": "todo"}
    ]

    # Events exist from creation.
    assert len(data["events"]) >= 1


def test_task_detail_and_board_surface_vault_memory_links(
    client, tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    note = vault / "00-Canon" / "vision.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Vision\nCanon note.\n", encoding="utf-8")
    pdf_note = vault / "00-Canon" / "brief.pdf"
    pdf_note.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    memory_note = Path(os.environ["HERMES_HOME"]) / "memories" / "MEMORY.md"
    memory_note.parent.mkdir(parents=True)
    memory_note.write_text("# Hermes Memory\nManual note.\n", encoding="utf-8")

    created = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "linked task",
            "body": f"Use [[#Local Heading]], [[00-Canon/vision|Vision]], [[00-Canon/brief.pdf|Brief]] and {memory_note}",
        },
    ).json()["task"]

    board = client.get(
        "/api/plugins/kanban/board",
        params={"card_body": "none", "card_diagnostics": "summary"},
    ).json()
    card = next(
        task
        for col in board["columns"]
        for task in col["tasks"]
        if task["id"] == created["id"]
    )
    assert [
        (link["kind"], link["display_path"]) for link in card["vault_memory_links"]
    ] == [
        ("vault", "00-Canon/vision.md"),
        ("vault", "00-Canon/brief.pdf"),
        ("memory", "MEMORY.md"),
    ]
    assert all(link["path"] != str(vault) for link in card["vault_memory_links"])
    card_links = {link["display_path"]: link for link in card["vault_memory_links"]}
    assert card_links["00-Canon/vision.md"]["url"]
    assert card_links["00-Canon/vision.md"]["obsidian_url"].startswith(
        "obsidian://open?"
    )
    assert card_links["00-Canon/brief.pdf"]["url"] is None
    assert card_links["00-Canon/brief.pdf"]["obsidian_url"].startswith(
        "obsidian://open?"
    )
    assert card_links["MEMORY.md"]["url"]

    detail = client.get(f"/api/plugins/kanban/tasks/{created['id']}").json()
    links = detail["task"]["vault_memory_links"]
    assert links[0]["obsidian_url"].startswith("obsidian://open?")
    assert links[1]["path"] == str(pdf_note)
    assert links[1]["url"] is None
    assert links[2]["path"] == str(memory_note)

    opened = client.get(links[0]["url"])
    assert opened.status_code == 200
    assert "Canon note." in opened.text


def test_task_detail_404_on_unknown(client):
    r = client.get("/api/plugins/kanban/tasks/does-not-exist")
    assert r.status_code == 404


def test_task_detail_includes_cost_usd_field(client):
    """K6: GET /tasks/:id surfaces a per-task cost_usd (sum over runs).
    Pre-K5a there is no cost recorded, so the field is present but None."""
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "costed"},
    ).json()["task"]
    data = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()
    assert "cost_usd" in data["task"]
    assert data["task"]["cost_usd"] is None


def test_stats_includes_k6_throughput_and_cost_keys(client):
    """K6: GET /stats additively exposes throughput/cycle-time/cost keys
    alongside the pre-existing per-status/per-assignee counts."""
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "done-one", "assignee": "x"},
    ).json()["task"]
    client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done"},
    )
    stats = client.get("/api/plugins/kanban/stats").json()
    # Pre-existing keys still present.
    assert "by_status" in stats and "by_assignee" in stats
    # New additive keys.
    for key in (
        "completed_last_24h",
        "completed_last_7d",
        "cycle_time_p50_seconds",
        "cycle_time_p90_seconds",
        "total_cost_usd_24h",
    ):
        assert key in stats
    assert stats["completed_last_24h"] >= 1
    assert stats["total_cost_usd_24h"] is None  # pre-K5a


def test_stats_autonomy_endpoint_counts_operator_escalations(client):
    conn = kb.connect()
    try:
        tasks = [
            kb.create_task(conn, title=f"task {i}", assignee="coder") for i in range(4)
        ]
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'operator_escalation', NULL, ?)",
            (tasks[0], int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/stats/autonomy").json()
    assert data == {
        "accepted_tasks": 4,
        "operator_escalations": 1,
        "autonomy_rate": 0.75,
    }


def test_stats_chain_completion_endpoint_counts_done_roots_with_all_leaves_done(client):
    conn = kb.connect()
    try:
        full_root = kb.create_task(conn, title="full root", assignee="orchestrator")
        full_leaf_a = kb.create_task(conn, title="full leaf a", assignee="coder")
        full_leaf_b = kb.create_task(conn, title="full leaf b", assignee="coder")
        partial_root = kb.create_task(
            conn, title="partial root", assignee="orchestrator"
        )
        partial_leaf_done = kb.create_task(
            conn, title="partial leaf done", assignee="coder"
        )
        partial_leaf_open = kb.create_task(
            conn, title="partial leaf open", assignee="coder"
        )
        for leaf in (full_leaf_a, full_leaf_b):
            kb.link_tasks(conn, leaf, full_root)
        for leaf in (partial_leaf_done, partial_leaf_open):
            kb.link_tasks(conn, leaf, partial_root)
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id IN (?, ?, ?, ?, ?)",
            (
                int(time.time()),
                full_root,
                full_leaf_a,
                full_leaf_b,
                partial_root,
                partial_leaf_done,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/stats/chain-completion").json()
    assert data == {
        "done_roots": 2,
        "completed_done_roots": 1,
        "chain_completion_rate": 0.5,
    }


def test_stats_payload_includes_queue_wait_p50(client, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 1_000)
    conn = kb.connect()
    try:
        task_ids = [
            kb.create_task(conn, title=f"queued {i}", assignee="coder")
            for i in range(3)
        ]
        for task_id, created_at, started_at in zip(
            task_ids, (100, 200, 300), (110, 230, 390)
        ):
            conn.execute(
                "UPDATE tasks SET created_at=? WHERE id=?", (created_at, task_id)
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at) VALUES (?, 'coder', 'running', NULL, ?)",
                (task_id, started_at),
            )
        conn.commit()
    finally:
        conn.close()

    stats = client.get("/api/plugins/kanban/stats").json()
    assert stats["oldest_ready_age_seconds"] == 900
    assert stats["queue_wait_p50_seconds"] == 30


def test_stats_payload_includes_run_duration_percentiles(client, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 10_000)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="duration seed", assignee="coder")
        for start, end in ((9_900, 9_910), (9_920, 9_940), (9_950, 9_980)):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) VALUES (?, 'coder', 'done', 'completed', ?, ?)",
                (task_id, start, end),
            )
        conn.commit()
    finally:
        conn.close()

    stats = client.get("/api/plugins/kanban/stats").json()
    assert stats["run_duration_percentiles"]["coder"] == {"p50": 20, "p90": 30, "n": 3}


def test_runs_summary_groups_completed_tree_by_root(client, kanban_home):
    """K7: a decomposed tree is summarised once at its root; interior work
    nodes are not listed as separate roots."""
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="ship feature", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="default",
            children=[
                {"title": "build A", "assignee": "coder", "parents": []},
                {"title": "build B", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        a, b = child_ids
        kb.complete_task(conn, a, summary="A done")
        kb.complete_task(conn, b, summary="B done")
        kb.complete_task(conn, root, summary="all merged")
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/runs/summary?since_hours=24").json()
    assert data["since_hours"] == 24
    assert data["completed_roots"] == 1
    assert len(data["roots"]) == 1
    only = data["roots"][0]
    assert only["id"] == root
    assert only["subtask_count"] == 2
    assert a not in [r["id"] for r in data["roots"]]
    # cycle-time present (non-negative); cost None pre-cost-data.
    assert only["cycle_time_seconds"] is not None and only["cycle_time_seconds"] >= 0
    assert "total_cost_usd" in data


def test_runs_summary_includes_effective_cost_for_subscription(client, kanban_home):
    """K7: runs_summary surfaces cost_effective_usd (real cost_usd +
    metadata.cost_usd_equivalent) so subscription chains (cost_usd=0) do not
    read as free."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="subscription ship", triage=True)
        (child,) = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="default",
            children=[{"title": "sub-build", "assignee": "claude-cli", "parents": []}],
            author="decomposer",
        )
        with kb.write_txn(conn):
            _insert_cost_run_with_meta(
                conn,
                child,
                profile="claude-cli",
                input_tokens=900,
                output_tokens=180,
                cost_usd=0.0,
                metadata={"cost_usd_equivalent": 0.42},
            )
        kb.complete_task(conn, child, summary="built")
        kb.complete_task(conn, root, summary="merged")

    data = client.get("/api/plugins/kanban/runs/summary?since_hours=24").json()
    assert "total_cost_effective_usd" in data
    assert data["total_cost_effective_usd"] == pytest.approx(0.42)
    only = next(r for r in data["roots"] if r["id"] == root)
    assert only["cost_usd"] == pytest.approx(0.0)
    assert only["cost_effective_usd"] == pytest.approx(0.42)


def test_runs_windowed_rollup_expands_workers_and_runners_by_effective_cost(
    client, kanban_home
):
    """S1: windowed rollup ranks roots by real+equivalent cost and keeps
    worker/runner provider+model evidence attached."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="mother rollup", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "metered worker", "assignee": "coder", "parents": []},
                {
                    "title": "subscription worker",
                    "assignee": "claude-cli",
                    "parents": [],
                },
            ],
            author="decomposer",
        )
        assert child_ids is not None
        worker_a, worker_b = child_ids
        low = kb.create_task(conn, title="lower cost root", assignee="coder")
        with kb.write_txn(conn):
            _insert_cost_run_with_meta(
                conn,
                worker_a,
                profile="coder",
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.20,
                metadata={"provider": "openrouter", "model": "qwen/qwen3-coder"},
            )
            _insert_cost_run_with_meta(
                conn,
                worker_b,
                profile="claude-cli",
                input_tokens=900,
                output_tokens=180,
                cost_usd=0.0,
                metadata={
                    "provider": "anthropic",
                    "model": "claude-opus-4-8",
                    "cost_usd_equivalent": 0.42,
                },
            )
            _insert_cost_run_with_meta(
                conn,
                low,
                profile="coder",
                input_tokens=50,
                output_tokens=10,
                cost_usd=0.05,
                metadata={"provider": "openrouter", "model": "qwen/qwen3-coder"},
            )
        kb.complete_task(conn, worker_a, summary="metered done")
        kb.complete_task(conn, worker_b, summary="subscription done")
        kb.complete_task(conn, root, summary="merged")
        kb.complete_task(conn, low, summary="smaller done")

    response = client.get("/api/plugins/kanban/runs/windowed-rollup?hours=24&limit=5")
    assert response.status_code == 200
    data = response.json()
    assert data["schema"] == "kanban-windowed-rollup-v1"
    assert data["since_hours"] == 24
    assert data["roots"][0]["id"] == root

    top = data["roots"][0]
    assert top["cost_usd"] == pytest.approx(0.20)
    assert top["cost_usd_equivalent"] == pytest.approx(0.42)
    assert top["cost_effective_usd"] == pytest.approx(0.62)
    assert top["providers"] == ["anthropic", "openrouter"]

    workers = {w["profile"]: w for w in top["workers"]}
    assert workers["coder"]["provider"] == "openrouter"
    assert workers["coder"]["model"] == "qwen/qwen3-coder"
    assert workers["claude-cli"]["provider"] == "anthropic"
    assert workers["claude-cli"]["model"] == "claude-opus-4-8"

    worker_a_runs = [r for r in top["runners"] if r["task_id"] == worker_a]
    worker_b_runs = [r for r in top["runners"] if r["task_id"] == worker_b]
    assert any(r["cost_effective_usd"] == pytest.approx(0.20) for r in worker_a_runs)
    assert any(r["cost_usd_equivalent"] == pytest.approx(0.42) for r in worker_b_runs)
    assert any(r["cost_effective_usd"] == pytest.approx(0.42) for r in worker_b_runs)


def test_runs_windowed_rollup_response_matches_frontend_zod_schema(client, kanban_home):
    """Contract: a real backend response must parse with the frontend Zod schema."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="contract mother", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "contract worker", "assignee": "coder", "parents": []}],
            author="decomposer",
        )
        assert child_ids is not None
        (worker,) = child_ids
        with kb.write_txn(conn):
            _insert_cost_run_with_meta(
                conn,
                worker,
                profile="coder",
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.20,
                metadata={"provider": "openrouter", "model": "qwen/qwen3-coder"},
            )
        kb.complete_task(conn, worker, summary="worker done")
        kb.complete_task(conn, root, summary="root done")

    response = client.get("/api/plugins/kanban/runs/windowed-rollup?hours=24&limit=5")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema"] == "kanban-windowed-rollup-v1"
    assert any(row["id"] == root for row in payload["roots"])

    repo_root = Path(__file__).resolve().parents[2]
    web_dir = repo_root / "web"
    if not (
        (web_dir / "node_modules" / "@nous-research" / "ui").exists()
        and (repo_root / "node_modules" / "vitest").exists()
    ):
        pytest.skip(
            "frontend dependencies are not installed; run npm ci in web/ for contract gate"
        )

    lib_dir = web_dir / "src" / "control" / "lib"
    fixture_file = lib_dir / "__windowed_rollup_contract_fixture.json"
    test_file = lib_dir / "__windowed_rollup_contract.generated.test.ts"
    fixture_file.write_text(json.dumps(payload), encoding="utf-8")
    test_file.write_text(
        'import { describe, expect, it } from "vitest";\n'
        'import fixture from "./__windowed_rollup_contract_fixture.json";\n'
        'import { WindowedRollupResponseSchema, parseOrThrow } from "./schemas";\n\n'
        'describe("windowed rollup backend/frontend contract", () => {\n'
        '  it("parses the real backend response with the frontend schema", () => {\n'
        '    const parsed = parseOrThrow(WindowedRollupResponseSchema, fixture, "windowed-rollup-contract");\n'
        '    expect(parsed.schema).toBe("kanban-windowed-rollup-v1");\n'
        "    expect(parsed.roots.length).toBeGreaterThan(0);\n"
        "    expect(parsed.roots[0].runners).toBeInstanceOf(Array);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            ["npm", "exec", "vitest", "--", "run", str(test_file.relative_to(web_dir))],
            cwd=web_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    finally:
        fixture_file.unlink(missing_ok=True)
        test_file.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout


def test_runs_windowed_rollup_keeps_unknown_costs_null(client, kanban_home):
    """S1: missing price evidence remains unknown/null instead of fake $0."""
    with kb.connect() as conn:
        root = kb.create_task(conn, title="unknown cost", assignee="coder")
        with kb.write_txn(conn):
            _insert_cost_run_with_meta(
                conn,
                root,
                profile="coder",
                input_tokens=12,
                output_tokens=3,
                cost_usd=None,
                metadata={"provider": "unknown", "model": "mystery"},
            )
        kb.complete_task(conn, root, summary="done")

    data = client.get("/api/plugins/kanban/runs/windowed-rollup?hours=24").json()
    only = next(r for r in data["roots"] if r["id"] == root)
    assert only["cost_usd"] is None
    assert only["cost_usd_equivalent"] is None
    assert only["cost_effective_usd"] is None
    assert only["runners"][0]["cost_effective_usd"] is None


def test_runs_windowed_rollup_exposes_runner_detail_metadata(client, kanban_home):
    """S3: detail rows carry real USD, billing_mode, provider/model and runtime."""
    import json as _json
    import time

    now = int(time.time())
    with kb.connect() as conn:
        root = kb.create_task(conn, title="detail root", triage=True)
        child = kb.create_task(conn, title="runner child", parents=[root])
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=? WHERE id IN (?, ?)",
                (now, root, child),
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, outcome, started_at, ended_at, metadata) "
                "VALUES (?, 'coder', 'done', 'completed', ?, ?, ?)",
                (
                    child,
                    now - 45,
                    now - 15,
                    _json.dumps({
                        "provider": "openrouter",
                        "model": "nous/hermes",
                        "cost_usd": 0.123456,
                        "cost_usd_equivalent": 0.75,
                        "billing_mode": "metered",
                    }),
                ),
            )

    response = client.get("/api/plugins/kanban/runs/windowed-rollup?hours=24")
    assert response.status_code == 200, response.text
    root_row = next(r for r in response.json()["roots"] if r["id"] == child)
    runner = root_row["runners"][0]

    assert runner["cost_usd"] == pytest.approx(0.123456)
    assert runner["cost_usd_equivalent"] == pytest.approx(0.75)
    assert runner["cost_effective_usd"] == pytest.approx(0.873456)
    assert runner["provider"] == "openrouter"
    assert runner["model"] == "nous/hermes"
    assert runner["billing_mode"] == "metered"
    assert runner["started_at"] == now - 45
    assert runner["ended_at"] == now - 15
    assert runner["runtime_seconds"] == 30
    assert runner["neuralwatt"] is None
    assert root_row["billing_mode"] == "metered"
    assert root_row["started_at"] == now - 45
    assert root_row["ended_at"] == now - 15
    assert root_row["runtime_seconds"] == 30
    assert root_row["neuralwatt"] is None


def test_runs_summary_empty_window(client):
    """K7: with nothing completed, the summary is well-formed and empty."""
    data = client.get("/api/plugins/kanban/runs/summary?since_hours=1").json()
    assert data["completed_roots"] == 0
    assert data["roots"] == []
    assert data["total_cost_usd"] is None


# ---------------------------------------------------------------------------
# Phase 3 (Statistik): /runs/reliability + /runs/daily
# ---------------------------------------------------------------------------


def _insert_run(
    conn,
    task_id,
    *,
    profile,
    outcome,
    started_at,
    ended_at,
    verdict=None,
    cost=None,
    tokens_in=None,
    tokens_out=None,
    metadata=None,
):
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, "
        "started_at, ended_at, verdict, cost_usd, input_tokens, "
        "output_tokens, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            profile,
            "done",
            outcome,
            started_at,
            ended_at,
            verdict,
            cost,
            tokens_in,
            tokens_out,
            json.dumps(metadata) if metadata is not None else None,
        ),
    )


def test_runs_reliability_per_profile(client):
    """Outcome-Raten pro Profil + Verdict-Zuordnung auf den GEPRÜFTEN Run:
    das Verdict steht auf dem Verifier-Run, gezählt wird es beim Profil des
    jüngsten zuvor beendeten verdict-freien Runs derselben Task."""
    now = int(time.time())
    conn = kb.connect()
    try:
        t1 = kb.create_task(conn, title="work 1")
        t2 = kb.create_task(conn, title="work 2")
        with kb.write_txn(conn):
            # coder: 2 completed, 1 crashed (3 runs, 2 tasks → 1 retry)
            _insert_run(
                conn,
                t1,
                profile="coder",
                outcome="crashed",
                started_at=now - 4000,
                ended_at=now - 3900,
            )
            _insert_run(
                conn,
                t1,
                profile="coder",
                outcome="completed",
                started_at=now - 3800,
                ended_at=now - 3600,
            )
            _insert_run(
                conn,
                t2,
                profile="coder",
                outcome="completed",
                started_at=now - 3000,
                ended_at=now - 2800,
            )
            # verifier judges t1 (APPROVED) and t2 (REQUEST_CHANGES) — the
            # verdicts must be attributed to coder, not verifier.
            _insert_run(
                conn,
                t1,
                profile="verifier",
                outcome="completed",
                started_at=now - 3500,
                ended_at=now - 3400,
                verdict="APPROVED",
            )
            _insert_run(
                conn,
                t2,
                profile="verifier",
                outcome="completed",
                started_at=now - 2700,
                ended_at=now - 2600,
                verdict="REQUEST_CHANGES",
            )
    finally:
        conn.close()

    data = client.get(
        "/api/plugins/kanban/runs/reliability?since_hours=24&min_n=2"
    ).json()
    assert data["min_n"] == 2
    by_profile = {p["profile"]: p for p in data["profiles"]}
    coder = by_profile["coder"]
    assert coder["runs"] == 3
    assert coder["outcomes"]["completed"] == 2
    assert coder["outcomes"]["crashed"] == 1
    assert coder["retries"] == 1
    assert coder["judged"] == 2
    assert coder["approved"] == 1 and coder["rejected"] == 1
    assert coder["approve_rate"] == 0.5
    assert coder["low_sample"] is False
    verifier = by_profile["verifier"]
    assert verifier["judged"] == 0  # verdicts never count for the verifier itself
    # min-n-Gate: unter der Schwelle keine approve_rate-Behauptung.
    data_strict = client.get(
        "/api/plugins/kanban/runs/reliability?since_hours=24&min_n=5"
    ).json()
    coder_strict = {p["profile"]: p for p in data_strict["profiles"]}["coder"]
    assert coder_strict["approve_rate"] is None
    assert coder_strict["low_sample"] is True


def test_runs_daily_series(client):
    """Tages-Zeitreihe: durchgehende Achse, Roots/Tasks/Kosten/Outcomes pro Tag."""
    now = int(time.time())
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="ship it", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="default",
            children=[{"title": "build", "assignee": "coder", "parents": []}],
            author="decomposer",
        )
        (a,) = child_ids
        kb.complete_task(conn, a, summary="done")
        kb.complete_task(conn, root, summary="done")
        with kb.write_txn(conn):
            _insert_run(
                conn,
                a,
                profile="coder",
                outcome="completed",
                started_at=now - 600,
                ended_at=now - 300,
                cost=0.25,
            )
            _insert_run(
                conn,
                a,
                profile="coder",
                outcome="crashed",
                started_at=now - 900,
                ended_at=now - 800,
                cost=0.05,
            )
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/runs/daily?days=7").json()
    assert data["days"] == 7
    assert len(data["series"]) == 7  # auch leere Tage (durchgehende Achse)
    active_days = [day for day in data["series"] if day["done_tasks"]]
    assert len(active_days) == 1
    today = active_days[0]
    assert today["done_roots"] == 1  # nur der Root zählt als Lieferung
    assert today["done_tasks"] == 2  # Root + Subtask
    # complete_task legt selbst synthetische completed-Runs an → >= statt ==.
    assert today["runs_completed"] >= 1
    assert today["runs_failed"] == 1
    assert today["cost_usd"] == 0.3
    assert today["cycle_time_p50_seconds"] is not None
    # leere Tage sind ehrlich leer
    assert data["series"][0]["done_tasks"] == 0
    assert data["series"][0]["cost_usd"] is None
    assert data["series"][0]["cycle_time_p50_seconds"] is None


def test_runs_daily_value_classes(client):
    """Wert-Bilanz: done_roots_by_class teilt gelieferte Roots nach
    created_by in nutzer (Funnel) / haertung (Review-Ketten) / meta (Rest)."""
    conn = kb.connect()
    try:
        for created_by in (
            "family",
            "discord-idee",
            "kanban-review-chain",
            "dashboard",
            "worker",
        ):
            tid = kb.create_task(
                conn,
                title=f"root {created_by}",
                created_by=created_by,
            )
            kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/runs/daily?days=7").json()
    today = data["series"][-1]
    assert today["done_roots"] == 5
    assert today["done_roots_by_class"] == {"nutzer": 2, "haertung": 1, "meta": 2}
    # leere Tage tragen die Klassen-Struktur mit Nullen (durchgehende Achse)
    assert data["series"][0]["done_roots_by_class"] == {
        "nutzer": 0,
        "haertung": 0,
        "meta": 0,
    }


def test_runs_costs_today_window_and_profiles(client):
    """F4: Kosten heute vs. Fenster + Top-Profile; echte Kosten bleiben von
    Abo/API-Äquivalenten und Neuralwatt-Abrechnungsbasis getrennt."""
    now = int(time.time())
    yesterday = now - 26 * 3600
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="costly work")
        with kb.write_txn(conn):
            # API-Lane heute: echte Dollar + Tokens.
            _insert_run(
                conn,
                t,
                profile="coder",
                outcome="completed",
                started_at=now - 600,
                ended_at=now,
                cost=0.25,
                tokens_in=1000,
                tokens_out=200,
            )
            # Subscription-Lane heute: ehrliche $0 + Äquivalent in Metadata.
            _insert_run(
                conn,
                t,
                profile="premium",
                outcome="completed",
                started_at=now - 500,
                ended_at=now,
                cost=0.0,
                tokens_in=5000,
                tokens_out=900,
                metadata={
                    "billing_mode": "subscription_included",
                    "cost_usd_equivalent": 1.5,
                },
            )
            # Neuralwatt-Lane: echte USD kommen direkt aus cost.request_cost_usd
            # (keine kWh×Rate-Rekonstruktion mehr); kWh bleibt als Energie-Detail
            # getrennt als Abrechnungsbasis sichtbar.
            _insert_run(
                conn,
                t,
                profile="neuralwatt",
                outcome="completed",
                started_at=now - 450,
                ended_at=now,
                cost=0.0,
                tokens_in=600,
                tokens_out=120,
                metadata={
                    "provider": "neuralwatt",
                    "energy": {"energy_kwh": 0.02},
                    "cost": {"request_cost_usd": 0.10},
                },
            )
            # Verifier ohne Stamps: zählt als Run, kostet nichts.
            _insert_run(
                conn,
                t,
                profile="verifier",
                outcome="completed",
                started_at=now - 400,
                ended_at=now,
            )
            # Gestern (im Fenster, nicht heute).
            _insert_run(
                conn,
                t,
                profile="coder",
                outcome="completed",
                started_at=yesterday - 300,
                ended_at=yesterday,
                cost=0.05,
                tokens_in=400,
                tokens_out=80,
            )
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/runs/costs?days=7").json()
    assert data["days"] == 7
    assert data["today"]["cost_usd"] == 0.25
    assert data["today"]["actual_cost_usd"] == pytest.approx(0.35)
    assert data["today"]["cost_usd_equivalent"] == 1.5
    assert data["today"]["api_equivalent_usd"] == 1.5
    assert data["today"]["billing_neuralwatt_kwh"] == pytest.approx(0.02)
    assert data["today"]["billing_neuralwatt_cost_usd"] == pytest.approx(0.10)
    assert data["today"]["runs"] == 4
    assert data["window"]["cost_usd"] == 0.3
    assert data["window"]["actual_cost_usd"] == pytest.approx(0.4)
    assert data["window"]["input_tokens"] == 7000
    by_profile = {p["profile"]: p for p in data["profiles"]}
    assert by_profile["coder"]["cost_usd"] == 0.3
    assert by_profile["coder"]["actual_cost_usd"] == pytest.approx(0.3)
    assert by_profile["coder"]["runs"] == 2
    assert by_profile["premium"]["cost_usd"] == 0.0
    assert by_profile["premium"]["cost_usd_equivalent"] == 1.5
    assert by_profile["premium"]["actual_cost_usd"] == pytest.approx(0.0)
    assert by_profile["neuralwatt"]["cost_usd"] == 0.0
    assert by_profile["neuralwatt"]["actual_cost_usd"] == pytest.approx(0.1)
    assert by_profile["neuralwatt"]["billing_neuralwatt_kwh"] == pytest.approx(0.02)
    assert by_profile["neuralwatt"]["billing_neuralwatt_cost_usd"] == pytest.approx(0.1)
    assert by_profile["verifier"]["cost_usd"] is None
    assert by_profile["verifier"]["actual_cost_usd"] is None
    assert by_profile["verifier"]["runs"] == 1
    # Sortierung: tatsächliche Kosten zuerst; Abo/API-Äquivalent sortiert nicht
    # vor echte Rechnungswerte.
    assert data["profiles"][0]["profile"] == "coder"
    assert data["profiles"][1]["profile"] == "neuralwatt"
    assert data["profiles"][2]["profile"] == "premium"
    # Fenster-Schnitt: days=1 sieht den gestrigen Run nicht mehr.
    narrow = client.get("/api/plugins/kanban/runs/costs?days=1").json()
    assert narrow["window"]["cost_usd"] == 0.25
    assert narrow["window"]["actual_cost_usd"] == pytest.approx(0.35)
    # Jede Profilzeile trägt das (server-aufgelöste) Abo-Lane-Feld mit.
    assert all("subscription" in p for p in data["profiles"])

    series = client.get("/api/plugins/kanban/runs/costs-series?days=7").json()
    today_key = time.strftime("%Y-%m-%d", time.localtime(now))
    today_point = next(p for p in series["series"] if p["day"] == today_key)
    assert series["field_sources"]["tokens"].startswith("task_runs.input_tokens")
    assert today_point["runs"] == 4
    assert today_point["input_tokens"] == 6600
    assert today_point["output_tokens"] == 1220
    assert today_point["total_tokens"] == 7820
    assert today_point["api_equivalent_usd"] == pytest.approx(1.5)


def test_runs_costs_review_value_real_metadata(client):
    """S1B/AC-2+AC-4: Review-Wert je Stufe aus dem ECHTEN
    ``task_runs.metadata``-Format. Die Fixtures sind aus realen Live-DB-Run-
    Datensätzen destilliert — verifier/reviewer tragen
    ``review_findings = {"blocking": <int>, "observations": <int>}`` neben den
    üblichen acceptance/cost/provider-Feldern (Form von Live-Runs 6011/6013),
    nicht synthetisch erfunden."""
    now = int(time.time())
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="reviewed work")
        with kb.write_txn(conn):
            # verifier — APPROVED, 1 Blocker + 2 Anmerkungen. Metadata-Form aus
            # Live-Run 6011 destilliert: review_findings neben
            # acceptance/cost/provider-Feldern.
            _insert_run(
                conn,
                t,
                profile="scout",
                outcome="completed",
                started_at=now - 1100,
                ended_at=now - 1000,
                tokens_in=35866,
                tokens_out=7000,
                metadata={
                    # Live-Run 6031-Form: read-only Scout hat keine
                    # review_findings, aber konkrete Leseevidenz.
                    "changed_files": [],
                    "files_read": [
                        "/home/piet/.hermes/hermes-agent/hermes_cli/control_plane_gate.py",
                    ],
                    "citations": [
                        "hermes_cli/control_plane_gate.py:178-188",
                        "hermes_cli/control_plane_gate.py:191-197",
                        "hermes_cli/control_plane_gate.py:200-217",
                    ],
                    "tests_run": 0,
                    "residual_risk": "read-only recon; no tests run",
                    "worker_session_id": "20260702_120856_55b0c5",
                },
            )
            # verifier — APPROVED, 1 Blocker + 2 Anmerkungen. Metadata-Form aus
            # Live-Run 6011 destilliert: review_findings neben
            # acceptance/cost/provider-Feldern.
            _insert_run(
                conn,
                t,
                profile="verifier",
                outcome="completed",
                started_at=now - 900,
                ended_at=now - 800,
                verdict="APPROVED",
                tokens_in=175738,
                tokens_out=8350,
                metadata={
                    "review_findings": {"blocking": 1, "observations": 2},
                    "acceptance_checklist": [
                        {"item": "AC-1", "verdict": "MET", "evidence": "…"},
                    ],
                    "caller_grep": "done",
                    "changed_files": ["hermes_cli/kanban_db.py"],
                    "cost_usd_equivalent": 2.1,
                    "model": "claude-opus-4-8",
                    "provider": "anthropic",
                    "subscription": "claude",
                    "tests_run": 3,
                    "worker_session_id": "20260702_x",
                },
            )
            # reviewer — APPROVED, nur eine Anmerkung (Live-Run 6013-Form).
            _insert_run(
                conn,
                t,
                profile="reviewer",
                outcome="completed",
                started_at=now - 700,
                ended_at=now - 600,
                verdict="APPROVED",
                tokens_in=30105,
                tokens_out=11231,
                metadata={
                    "review_findings": {"blocking": 0, "observations": 1},
                    "independent_finding": "one nit",
                    "checked_files": ["a.py"],
                    "verdict": "APPROVED",
                    "worker_session_id": "20260702_y",
                },
            )
            # reviewer — REQUEST_CHANGES mit 2 Blockern.
            _insert_run(
                conn,
                t,
                profile="reviewer",
                outcome="completed",
                started_at=now - 500,
                ended_at=now - 400,
                verdict="REQUEST_CHANGES",
                tokens_in=42000,
                tokens_out=6000,
                metadata={
                    "review_findings": {"blocking": 2, "observations": 0},
                    "verdict": "REQUEST_CHANGES",
                },
            )
            # critic — Feld vorhanden, aber nachweislich KEINE Funde (0/0):
            # Fund-Zähler stehen auf 0, tokens_per_finding bleibt None.
            _insert_run(
                conn,
                t,
                profile="critic",
                outcome="completed",
                started_at=now - 300,
                ended_at=now - 200,
                verdict="APPROVED",
                tokens_in=48931,
                tokens_out=2170,
                metadata={
                    "review_findings": {"blocking": 0, "observations": 0},
                    "action": "uphold",
                    "critic_checks": {},
                },
            )
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/runs/costs?days=7").json()
    stages = {s["profile"]: s for s in data["review_value"]}
    assert set(stages) == {"scout", "verifier", "reviewer", "critic"}

    scout = stages["scout"]
    assert scout["runs"] == 1
    assert scout["findings_blocking"] is None
    assert scout["findings_observations"] is None
    assert scout["tokens_per_finding"] is None
    assert scout["read_items"] == 4
    assert scout["tokens_per_read_item"] == round(35866 / 4)

    verifier = stages["verifier"]
    assert verifier["runs"] == 1
    assert verifier["approved"] == 1
    assert verifier["request_changes"] == 0
    assert verifier["findings_blocking"] == 1
    assert verifier["findings_observations"] == 2
    assert verifier["input_tokens"] == 175738
    assert verifier["tokens_per_finding"] == round(175738 / 3)
    assert verifier["read_items"] is None
    assert verifier["tokens_per_read_item"] is None

    reviewer = stages["reviewer"]
    assert reviewer["runs"] == 2
    assert reviewer["approved"] == 1
    assert reviewer["request_changes"] == 1
    assert reviewer["findings_blocking"] == 2  # 0 + 2
    assert reviewer["findings_observations"] == 1  # 1 + 0
    assert reviewer["input_tokens"] == 30105 + 42000
    assert reviewer["tokens_per_finding"] == round((30105 + 42000) / 3)

    # critic: Feld vorhanden mit 0 Funden → Zähler 0, aber keine Kosten-pro-Fund.
    critic = stages["critic"]
    assert critic["runs"] == 1
    assert critic["approved"] == 1
    assert critic["findings_blocking"] == 0
    assert critic["findings_observations"] == 0
    assert critic["tokens_per_finding"] is None
    assert critic["input_tokens"] == 48931


def test_runs_costs_review_value_legacy_without_field_is_null(client):
    """S1B/AC-1+AC-4: Altbestand ohne ``metadata.review_findings`` ergibt ein
    NULL-Fund-Aggregat je Stufe — Läufe und Verdikte werden weiter gezählt, die
    Fund-Felder und tokens_per_finding bleiben None, niemals 0 und niemals ein
    Fehler. Eine Stufe ganz ohne Läufe ist ebenfalls NULL."""
    now = int(time.time())
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="legacy review")
        with kb.write_txn(conn):
            _insert_run(
                conn,
                t,
                profile="verifier",
                outcome="completed",
                started_at=now - 600,
                ended_at=now - 500,
                verdict="APPROVED",
                tokens_in=90000,
                tokens_out=4000,
                metadata={"cost_usd_equivalent": 1.0, "model": "x"},
            )
            _insert_run(
                conn,
                t,
                profile="reviewer",
                outcome="completed",
                started_at=now - 400,
                ended_at=now - 300,
                verdict="REQUEST_CHANGES",
                tokens_in=50000,
                tokens_out=3000,
                metadata={"verdict": "REQUEST_CHANGES"},
            )
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/runs/costs?days=7").json()
    stages = {s["profile"]: s for s in data["review_value"]}

    verifier = stages["verifier"]
    assert verifier["runs"] == 1
    assert verifier["approved"] == 1
    assert verifier["findings_blocking"] is None
    assert verifier["findings_observations"] is None
    assert verifier["tokens_per_finding"] is None
    assert verifier["input_tokens"] == 90000

    reviewer = stages["reviewer"]
    assert reviewer["runs"] == 1
    assert reviewer["request_changes"] == 1
    assert reviewer["findings_blocking"] is None
    assert reviewer["tokens_per_finding"] is None

    # critic: keine Läufe im Fenster → leere Stufe, ebenfalls NULL, kein Fehler.
    scout = stages["scout"]
    assert scout["runs"] == 0
    assert scout["findings_blocking"] is None
    assert scout["read_items"] is None
    assert scout["tokens_per_read_item"] is None

    # critic: keine Läufe im Fenster → leere Stufe, ebenfalls NULL, kein Fehler.
    critic = stages["critic"]
    assert critic["runs"] == 0
    assert critic["findings_blocking"] is None
    assert critic["findings_observations"] is None
    assert critic["tokens_per_finding"] is None


def test_profile_subscription_grounded_in_provider(monkeypatch):
    """Abo-Lane für den Statistik-Panel wird aus Runtime/Provider aufgelöst,
    NICHT aus dem Profilnamen — so kann eine umbenannte/umgewidmete Lane (Kimi
    läuft auf ``reviewer``, Codex auf ``verifier``) nicht falsch zugeordnet
    werden."""
    kb._PROFILE_SUBSCRIPTION_CACHE.clear()
    # claude-cli-Runtime (hier via Env-Allowlist) → Claude Max, unabhängig vom
    # model.provider (premium fährt ein Codex-Modell durch die claude-cli-Lane).
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "premium")
    assert kb._profile_subscription("premium") == "claude"

    monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
    monkeypatch.setattr(kb, "_is_claude_cli_runtime", lambda p: False)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda p: "/x")
    cases = {
        "reviewer": ("kimi-coding", "kimi"),
        "coder": ("openai-codex", "chatgpt"),
        "verifier": ("openai-codex", "chatgpt"),
        "coder-claude": ("anthropic", "claude"),
        "critic": ("openrouter", None),  # API-Lane, kein Abo
        "research": ("gemini", None),  # API-Lane, kein Abo
    }
    for name, (prov, expect) in cases.items():
        kb._PROFILE_SUBSCRIPTION_CACHE.clear()
        monkeypatch.setattr(kb, "_read_profile_provider", lambda home, _p=prov: _p)
        assert kb._profile_subscription(name) == expect, name

    # Leere/synthetische Profilnamen sind kein Abo.
    for junk in ["", "   ", "(ohne profil)", None]:
        assert kb._profile_subscription(junk) is None


# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------


def test_patch_status_complete(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done", "result": "shipped"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "done"

    # Board reflects the move.
    done = next(
        c
        for c in client.get("/api/plugins/kanban/board").json()["columns"]
        if c["name"] == "done"
    )
    assert any(x["id"] == t["id"] for x in done["tasks"])


def test_patch_block_then_unblock(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "blocked", "block_reason": "need input"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "blocked"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "ready"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "ready"


def test_patch_schedule_then_unblock(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "scheduled", "block_reason": "run tomorrow"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "scheduled"

    columns = client.get("/api/plugins/kanban/board").json()["columns"]
    assert "scheduled" in [c["name"] for c in columns]
    scheduled = next(c for c in columns if c["name"] == "scheduled")
    assert any(x["id"] == t["id"] for x in scheduled["tasks"])

    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "ready"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "ready"


def test_patch_drag_drop_move_todo_to_ready(client):
    """Direct status write: the drag-drop path for statuses without a
    dedicated verb (e.g. manually promoting todo -> ready).

    Promoting a child whose parent is not done is rejected (409).
    Promoting a child whose parent IS done is accepted (200)."""
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()[
        "task"
    ]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    # Rejected: parent not done yet.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{child['id']}",
        json={"status": "ready"},
    )
    assert r.status_code == 409

    # The 409 detail must name the blocking parent so the dashboard can
    # render an actionable toast instead of a silent no-op (#26744).
    detail = r.json()["detail"]
    assert "Cannot move to 'ready'" in detail
    assert parent["id"] in detail
    assert "'p'" in detail
    assert "status=" in detail
    # Whatever non-``done`` status the parent currently has must show up
    # so the operator knows what to fix.
    assert f"status={parent['status']}" in detail
    assert parent["status"] != "done"

    # Complete the parent.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    # Now child auto-promoted by recompute_ready — already ready.
    child_after = client.get(f"/api/plugins/kanban/tasks/{child['id']}").json()["task"]
    assert child_after["status"] == "ready"


def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()[
        "task"
    ]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(f"/api/plugins/kanban/tasks/{child['id']}").json()[
        "task"
    ]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(f"/api/plugins/kanban/tasks/{child['id']}").json()[
        "task"
    ]
    assert child_after_reopen["status"] == "todo"


def test_patch_reassign(client):
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "assignee": "a"},
    ).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"assignee": "b"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["assignee"] == "b"


def test_patch_priority_and_edit(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"priority": 5, "title": "renamed"},
    )
    assert r.status_code == 200
    data = r.json()["task"]
    assert data["priority"] == 5
    assert data["title"] == "renamed"


def test_patch_invalid_status(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "banana"},
    )
    assert r.status_code == 400


def test_patch_status_running_rejected(client):
    """Dashboard PATCH cannot transition a task directly to 'running'.

    The only legitimate path into 'running' is through the dispatcher's
    ``claim_task`` — which atomically creates a ``task_runs`` row,
    claim_lock, expiry, and worker-PID metadata. Allowing a direct set
    creates orphaned 'running' tasks with no run row or claim, which
    violate the board's run-history invariants. See issue #19535.
    """
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "running"},
    )
    assert r.status_code == 400
    assert "running" in r.json()["detail"]
    # Task's status should still be its pre-request value — the direct-set
    # was rejected before any mutation.
    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"] for col in board["columns"] for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------


def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()[
        "task"
    ]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404


def test_delete_task_not_found(client):
    r = client.delete("/api/plugins/kanban/tasks/t_nonexistent")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------


def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"


def test_add_comment_empty_rejected(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "   "},
    )
    assert r.status_code == 400


def test_add_link_and_delete_link(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/links",
        json={"parent_id": a["id"], "child_id": b["id"]},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{b['id']}")
    assert a["id"] in r.json()["links"]["parents"]

    r = client.delete(
        "/api/plugins/kanban/links",
        params={"parent_id": a["id"], "child_id": b["id"]},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_add_link_cycle_rejected(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    client.post(
        "/api/plugins/kanban/links",
        json={"parent_id": a["id"], "child_id": b["id"]},
    )
    r = client.post(
        "/api/plugins/kanban/links",
        json={"parent_id": b["id"], "child_id": a["id"]},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------


def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------


def test_create_triage_lands_in_triage_column(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "rough idea, spec me", "triage": True},
    )
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["status"] == "triage"

    r = client.get("/api/plugins/kanban/board")
    triage = next(c for c in r.json()["columns"] if c["name"] == "triage")
    assert len(triage["tasks"]) == 1
    assert triage["tasks"][0]["title"] == "rough idea, spec me"


def test_triage_task_not_promoted_to_ready(client):
    """Triage tasks must stay in triage even when they have no parents."""
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "must stay put", "triage": True},
    )
    # Run the dispatcher — it should NOT promote the triage task.
    client.post("/api/plugins/kanban/dispatch?dry_run=false&max=4")
    r = client.get("/api/plugins/kanban/board")
    triage = next(c for c in r.json()["columns"] if c["name"] == "triage")
    ready = next(c for c in r.json()["columns"] if c["name"] == "ready")
    assert len(triage["tasks"]) == 1
    assert len(ready["tasks"]) == 0


def test_patch_status_triage_works(client):
    """A user (or specifier) can push a task back into triage, and out of it."""
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x"},
    ).json()["task"]
    # Normal creation is 'ready'; push to triage.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "triage"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "triage"

    # Now promote to todo.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "todo"


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


def test_board_progress_rollup(client):
    parent = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "parent"},
    ).json()["task"]
    child_a = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "a", "parents": [parent["id"]]},
    ).json()["task"]
    child_b = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "b", "parents": [parent["id"]]},
    ).json()["task"]
    # Children start as "todo" because the parent isn't done yet.  Set the
    # parent to done so children auto-promote to ready via recompute_ready.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200
    # Verify children are now ready.
    for cid in (child_a["id"], child_b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{cid}").json()["task"]
        assert t["status"] == "ready", f"{cid} should be ready after parent done"

    # 0/2 done.
    r = client.get("/api/plugins/kanban/board")
    parent_row = next(
        t
        for col in r.json()["columns"]
        for t in col["tasks"]
        if t["id"] == parent["id"]
    )
    assert parent_row["progress"] == {"done": 0, "total": 2}

    # Complete one child. 1/2.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{child_a['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200
    r = client.get("/api/plugins/kanban/board")
    parent_row = next(
        t
        for col in r.json()["columns"]
        for t in col["tasks"]
        if t["id"] == parent["id"]
    )
    assert parent_row["progress"] == {"done": 1, "total": 2}

    # Childless tasks report progress=None, not {0/0}.
    assert (
        next(
            t
            for col in r.json()["columns"]
            for t in col["tasks"]
            if t["id"] == child_b["id"]
        )["progress"]
        is None
    )


# ---------------------------------------------------------------------------
# Board cards carry their chain root (root_id)
# ---------------------------------------------------------------------------


def test_board_root_id_resolves_chain(client):
    """Every card resolves its chain ROOT (the tree sink). Link convention:
    a child waits for its parent — the work tasks are PARENTS of the sink,
    so the sink is reached by walking child links. Standalone tasks are
    their own root."""
    # sink waits for two work tasks: work_a, work_b are parents of sink.
    work_a = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work a"},
    ).json()["task"]
    work_b = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work b"},
    ).json()["task"]
    sink = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "sink", "parents": [work_a["id"], work_b["id"]]},
    ).json()["task"]
    lone = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "standalone"},
    ).json()["task"]

    cards = {
        t["id"]: t
        for col in client.get("/api/plugins/kanban/board").json()["columns"]
        for t in col["tasks"]
    }
    assert cards[work_a["id"]]["root_id"] == sink["id"]
    assert cards[work_b["id"]]["root_id"] == sink["id"]
    assert cards[sink["id"]]["root_id"] == sink["id"]
    assert cards[lone["id"]]["root_id"] == lone["id"]


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


def test_board_auto_initializes_missing_db(tmp_path, monkeypatch):
    """If kanban.db doesn't exist yet, GET /board must create it, not 500."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Deliberately DO NOT call kb.init_db().

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)
    r = c.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    assert (home / "kanban.db").exists(), "init_db wasn't invoked by /board"


# ---------------------------------------------------------------------------
# WebSocket auth
# ---------------------------------------------------------------------------


def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    with c.websocket_connect("/api/plugins/kanban/events?token=secret-xyz") as ws:
        assert ws is not None


def test_ws_events_accepts_gated_ticket(tmp_path, monkeypatch):
    """Gated OAuth mode: the WS must accept a single-use ?ticket= (and reject
    a bare ?token=, even one matching _SESSION_TOKEN). This is the regression
    for the hosted-dashboard bug where the kanban live-events WS 1008'd on
    every gated deployment because its bespoke check only knew _SESSION_TOKEN.
    We stub _ws_auth_ok with the real gated semantics (ticket-only)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        # Gated mode: only a known ticket is accepted; token path rejected.
        return ws.query_params.get("ticket", "") == "good-ticket"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    from starlette.websockets import WebSocketDisconnect

    # Legacy token is rejected in gated mode, even if it's the real one.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=secret-xyz"):
            pass
    assert exc.value.code == 1008

    # A valid ticket is accepted.
    with c.websocket_connect("/api/plugins/kanban/events?ticket=good-ticket") as ws:
        assert ws is not None


def test_ws_events_board_query_param_default_overrides_current_board_pointer(
    tmp_path, monkeypatch
):
    """The event stream must honor ``board=default`` even when the global
    current-board pointer targets a different board.

    This is the live-update half of the dashboard regression: after the UI
    selects Default, the websocket must not subscribe to the CLI's current
    non-default board.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    default_conn = kb.connect()
    try:
        default_task = kb.create_task(default_conn, title="default-live")
    finally:
        default_conn.close()

    kb.create_board("other")
    other_conn = kb.connect(board="other")
    try:
        other_task = kb.create_task(other_conn, title="other-live")
    finally:
        other_conn.close()

    kb.set_current_board("other")

    import hermes_cli
    import types

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=lambda ws: ws.query_params.get("token", "") == "secret-xyz",
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz&board=default&since=0"
    ) as ws:
        payload = ws.receive_json()

    task_ids = {event["task_id"] for event in payload["events"]}
    assert default_task in task_ids
    assert other_task not in task_ids


def test_ws_events_swallows_cancellation_on_shutdown(tmp_path, monkeypatch):
    """``asyncio.CancelledError`` while sleeping in the poll loop is the
    normal uvicorn-shutdown path (``BaseException``, so the bare
    ``except Exception:`` does NOT catch it). Without the explicit
    clause the cancellation surfaces as an application traceback.

    Regression test for #20790 (fix in #20938). Drives the coroutine
    directly (rather than through FastAPI TestClient) so we can observe
    the cancellation outcome deterministically.
    """
    import asyncio

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Short-circuit the auth check — this test is about the cancellation
    # path, not auth.
    import plugins.kanban.dashboard.plugin_api as pa

    monkeypatch.setattr(pa, "_ws_upgrade_authorized", lambda ws: True)

    class _FakeWS:
        def __init__(self):
            self.query_params = {"token": "x", "since": "0"}
            self.accepted = False
            self.closed = False

        async def accept(self):
            self.accepted = True

        async def send_json(self, data):
            pass

        async def close(self, code=None):
            self.closed = True

    async def _run():
        ws = _FakeWS()
        task = asyncio.create_task(pa.stream_events(ws))
        # Give the handler a tick to accept + start polling.
        await asyncio.sleep(0.05)
        assert ws.accepted is True
        task.cancel()
        # stream_events should swallow CancelledError and return cleanly.
        # If it doesn't, this await re-raises the CancelledError.
        result = await task
        return result, ws

    result, ws = asyncio.run(_run())
    assert result is None, (
        f"stream_events should return cleanly after cancellation, got {result!r}"
    )
    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(
            f"/api/plugins/kanban/tasks/{tid}",
            json={"status": "blocked", "block_reason": "wait"},
        )

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert all(r["ok"] for r in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {t["id"] for t in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


def test_bulk_status_done_forwards_completion_summary(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [a["id"], b["id"]],
            "status": "done",
            "result": "DECIDED: ship it",
            "summary": "DECIDED: ship it",
            "metadata": {"source": "dashboard"},
        },
    )

    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    conn = kb.connect()
    try:
        for tid in (a["id"], b["id"]):
            task = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            assert task.status == "done"
            assert task.result == "DECIDED: ship it"
            assert run.summary == "DECIDED: ship it"
            assert run.metadata == {"source": "dashboard"}
    finally:
        conn.close()


def test_bulk_status_running_rejected(client):
    """Bulk updates must match single-task PATCH: direct 'running' is invalid."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [t["id"]], "status": "running"},
    )

    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["id"] == t["id"]
    assert results[0]["ok"] is False
    assert "running" in results[0]["error"]

    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"] for col in board["columns"] for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"


def test_dashboard_done_actions_prompt_for_completion_summary():
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert "withCompletionSummary" in bundle
    assert "Completion summary" in bundle
    assert "result: summary" in bundle
    assert "body: JSON.stringify(patch)" in bundle
    assert "body: JSON.stringify(finalPatch)" in bundle


def test_dashboard_surfaces_ready_blocked_error_inline():
    """Regression for #26744: failed status transitions must be surfaced
    inline, not swallowed.  The drag/drop banner and the drawer's action
    row each render the parsed API ``detail`` so operators see *why*
    their click did nothing.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    # Helper that strips ``"409: {\"detail\":\"…\"}"`` down to the
    # human-readable message before it lands in any banner.
    assert "function parseApiErrorMessage(err)" in bundle
    assert "parsed.detail" in bundle

    # Drag/drop banner now uses the parsed message instead of raw
    # ``err.message`` so it no longer leaks HTTP plumbing.
    assert (
        'setError(tx(t, "moveFailed", "Move failed: ") + parseApiErrorMessage(err))'
        in bundle
    )

    # Drawer action row has its own visible error surface and clears it
    # on success/refresh so stale failures don't follow the operator
    # around.
    assert "const [patchErr, setPatchErr] = useState(null);" in bundle
    assert "setPatchErr(parseApiErrorMessage(e))" in bundle
    assert "setPatchErr(null)" in bundle


def test_dashboard_dependency_selects_use_value_change_handler():
    """Regression for the dependency selects in the task drawer: the
    add-parent / add-child dropdowns must wire through the shared
    selectChangeHandler helper so their value actually lands on the
    underlying React state. Salvaged from #20019 @LeonSGP43.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    parent_select = (
        "value: newParent,\n"
        '          className: "h-7 text-xs flex-1",\n'
        "        }, selectChangeHandler(setNewParent))"
    )
    child_select = (
        "value: newChild,\n"
        '          className: "h-7 text-xs flex-1",\n'
        "        }, selectChangeHandler(setNewChild))"
    )

    assert parent_select in bundle
    assert child_select in bundle


def test_bulk_archive(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"]], "archive": True},
    )
    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    # Default board (archived hidden) — both gone.
    board = client.get("/api/plugins/kanban/board").json()
    ids = {t["id"] for col in board["columns"] for t in col["tasks"]}
    assert a["id"] not in ids
    assert b["id"] not in ids


def test_bulk_reassign(client):
    a = client.post(
        "/api/plugins/kanban/tasks", json={"title": "a", "assignee": "old"}
    ).json()["task"]
    b = client.post(
        "/api/plugins/kanban/tasks", json={"title": "b", "assignee": "old"}
    ).json()["task"]
    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"]], "assignee": "new"},
    )
    assert r.status_code == 200
    for tid in (a["id"], b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["assignee"] == "new"


def test_bulk_unassign_via_empty_string(client):
    a = client.post(
        "/api/plugins/kanban/tasks", json={"title": "a", "assignee": "x"}
    ).json()["task"]
    r = client.post(
        "/api/plugins/kanban/tasks/bulk", json={"ids": [a["id"]], "assignee": ""}
    )
    assert r.status_code == 200
    t = client.get(f"/api/plugins/kanban/tasks/{a['id']}").json()["task"]
    assert t["assignee"] is None


def test_bulk_partial_failure_doesnt_abort_siblings(client):
    """One bad id in the middle of a batch must not prevent others from
    applying."""
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], "bogus-id", c2["id"]], "priority": 7},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    ok_ids = {r["id"] for r in results if r["ok"]}
    assert a["id"] in ok_ids
    assert c2["id"] in ok_ids
    assert any(not r["ok"] and r["id"] == "bogus-id" for r in results)
    # Good siblings actually got the priority bump.
    for tid in (a["id"], c2["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["priority"] == 7


def test_bulk_empty_ids_400(client):
    r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


def test_config_returns_defaults_when_section_missing(client):
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    # Defaults when dashboard.kanban is missing.
    assert data["default_tenant"] == ""
    assert data["lane_by_profile"] is True
    assert data["include_archived_by_default"] is False
    assert data["render_markdown"] is True


def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False


def test_mutating_endpoint_500_uses_generic_detail(client, monkeypatch):
    from hermes_cli import profiles as profiles_mod

    secret = "boom /tmp/private/profile.yaml Traceback"

    def _raise(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(profiles_mod, "write_profile_meta", _raise)

    r = client.patch(
        "/api/plugins/kanban/profiles/default",
        json={"description": "new operator-authored profile text"},
    )

    assert r.status_code == 500
    body = r.text
    assert "failed to update profile" in body
    assert "boom" not in body
    assert "/tmp/private/profile.yaml" not in body
    assert "Traceback" not in body


# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------


def test_task_detail_includes_runs(client):
    """GET /tasks/:id carries a runs[] array with the attempt history."""
    r = client.post(
        "/api/plugins/kanban/tasks", json={"title": "port x", "assignee": "worker"}
    ).json()
    tid = r["task"]["id"]

    # Drive status running to force a run creation: PATCH to running
    # doesn't call claim_task (the PATCH path uses _set_status_direct),
    # so use the bulk/claim indirection via the kernel.
    import hermes_cli.kanban_db as _kb

    conn = _kb.connect()
    try:
        _kb.claim_task(conn, tid)
        _kb.complete_task(
            conn,
            tid,
            result="done",
            summary="tested on rate limiter",
            metadata={"changed_files": ["limiter.py"]},
        )
    finally:
        conn.close()

    d = client.get(f"/api/plugins/kanban/tasks/{tid}").json()
    assert "runs" in d
    assert len(d["runs"]) == 1
    run = d["runs"][0]
    assert run["outcome"] == "completed"
    assert run["profile"] == "worker"
    assert run["summary"] == "tested on rate limiter"
    md = {k: v for k, v in run["metadata"].items() if k != "cost"}
    assert md == {"changed_files": ["limiter.py"]}
    assert run["ended_at"] is not None


def test_task_detail_runs_empty_before_claim(client):
    """A task that's never been claimed has an empty runs[] list, not
    a missing key."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "fresh"}).json()
    d = client.get(f"/api/plugins/kanban/tasks/{r['task']['id']}").json()
    assert d["runs"] == []


def test_task_deliverables_lists_result_md_first_and_downloads_safe_file(
    client, kanban_home
):
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "deliverable"}
    ).json()["task"]
    root = kanban_home / "reports" / "by-task" / task["id"]
    nested = root / "artifacts"
    nested.mkdir(parents=True)
    (root / "RESULT.md").write_text("# Result\nreal worker output\n", encoding="utf-8")
    (nested / "notes.txt").write_text("supporting artifact", encoding="utf-8")

    r = client.get(f"/api/plugins/kanban/tasks/{task['id']}/deliverables")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    assert [item["relative_path"] for item in data["deliverables"]] == [
        "RESULT.md",
        "artifacts/notes.txt",
    ]
    result_md = data["deliverables"][0]
    assert result_md["filename"] == "RESULT.md"
    assert result_md["content_type"] == "text/markdown"
    assert result_md["url"].endswith(f"/tasks/{task['id']}/deliverables/RESULT.md")

    download = client.get(result_md["url"])
    assert download.text == "# Result\nreal worker output\n"
    assert download.headers["content-disposition"].startswith("inline")


def test_task_deliverables_rejects_traversal_and_skips_symlinks_outside(
    client, kanban_home
):
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "safe deliverables"}
    ).json()["task"]
    root = kanban_home / "reports" / "by-task" / task["id"]
    root.mkdir(parents=True)
    outside = kanban_home / "reports" / "by-task" / "outside-secret.txt"
    outside.write_text("do not serve", encoding="utf-8")
    (root / "RESULT.md").write_text("ok", encoding="utf-8")
    try:
        (root / "leak.txt").symlink_to(outside)
    except OSError:
        pass

    listed = client.get(f"/api/plugins/kanban/tasks/{task['id']}/deliverables").json()[
        "deliverables"
    ]
    assert [item["relative_path"] for item in listed] == ["RESULT.md"]

    escaped = client.get(
        f"/api/plugins/kanban/tasks/{task['id']}/deliverables/%2e%2e/outside-secret.txt"
    )
    assert escaped.status_code == 404
    assert "do not serve" not in escaped.text


def test_task_deliverables_caps_large_artifact_trees_and_keeps_result_md(
    client,
    kanban_home,
    monkeypatch,
):
    plugin_api = sys.modules["hermes_dashboard_plugin_kanban_test"]

    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "many deliverables"}
    ).json()["task"]
    root = kanban_home / "reports" / "by-task" / task["id"]
    root.mkdir(parents=True)
    (root / "RESULT.md").write_text("primary result", encoding="utf-8")
    for idx in range(20):
        (root / f"artifact-{idx:02d}.txt").write_text(str(idx), encoding="utf-8")

    monkeypatch.setattr(plugin_api, "_DELIVERABLES_MAX_SCANNED", 4)
    monkeypatch.setattr(plugin_api, "_DELIVERABLES_MAX_FILES", 4)

    r = client.get(f"/api/plugins/kanban/tasks/{task['id']}/deliverables")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["count"] == 4
    assert data["deliverables"][0]["relative_path"] == "RESULT.md"
    assert all(
        item["relative_path"].startswith(("RESULT.md", "artifact-"))
        for item in data["deliverables"]
    )


def test_recent_results_includes_preserved_deliverables(client, kanban_home):
    now = int(time.time())
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="has preserved files", assignee="coder")
        _insert_completed_run(
            conn,
            task_id=task_id,
            title="has preserved files",
            started_at=now - 20,
            ended_at=now - 10,
            summary="completed with RESULT.md",
        )
        conn.commit()
    finally:
        conn.close()

    root = kanban_home / "reports" / "by-task" / task_id
    root.mkdir(parents=True)
    (root / "artifact.json").write_text("{}", encoding="utf-8")
    (root / "RESULT.md").write_text("# Done", encoding="utf-8")

    r = client.get("/api/plugins/kanban/runs/recent-results")
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["task_id"] == task_id
    assert [item["relative_path"] for item in result["deliverables"]] == [
        "RESULT.md",
        "artifact.json",
    ]
    assert result["deliverables"][0]["url"].endswith(
        f"/tasks/{task_id}/deliverables/RESULT.md"
    )


def test_recent_results_exposes_openable_artifact_links_from_metadata_and_preserved_event(
    client, kanban_home
):
    now = int(time.time())
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="artifact-linked result", assignee="coder")
        reports_root = kanban_home / "reports" / "by-task" / task_id
        reports_result = reports_root / "RESULT.md"
        workspace_result = kanban_home / "kanban" / "workspaces" / task_id / "RESULT.md"
        run_id = _insert_completed_run(
            conn,
            task_id=task_id,
            title="artifact-linked result",
            started_at=now - 20,
            ended_at=now - 10,
            summary="APPROVED — Verifier-Run 944 checked RESULT.md",
            metadata={
                "verdict": "APPROVED",
                "artifacts": [str(reports_result), str(workspace_result)],
            },
            profile="verifier",
        )
        _append_claimed_event(
            conn,
            task_id=task_id,
            run_id=run_id,
            payload={"run_id": run_id, "source_status": "review"},
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'deliverables_preserved', ?, ?)",
            (
                task_id,
                json.dumps({
                    "dir": str(reports_root),
                    "files": ["RESULT.md", "PRESERVED.md"],
                }),
                now - 5,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reports_root.mkdir(parents=True)
    reports_result.write_text("# Result\nVerifier approved.\n", encoding="utf-8")
    (reports_root / "PRESERVED.md").write_text("# Preserved\n", encoding="utf-8")

    r = client.get("/api/plugins/kanban/runs/recent-results")
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["task_id"] == task_id
    assert result["verification_state"] == "approved"
    assert result["verifier_verdict"] == "APPROVED"
    assert str(reports_result) in result["artifacts"]
    links = result["artifact_links"]
    assert links, result
    result_link = next(link for link in links if link["relative_path"] == "RESULT.md")
    assert result_link["filename"] == "RESULT.md"
    assert result_link["url"].endswith(f"/tasks/{task_id}/deliverables/RESULT.md")
    assert result_link["source"] == "metadata.artifacts"
    assert result_link["path"] == str(reports_result)
    preserved_link = next(
        link for link in links if link["relative_path"] == "PRESERVED.md"
    )
    assert preserved_link["source"] == "deliverables_preserved"
    assert preserved_link["url"].endswith(f"/tasks/{task_id}/deliverables/PRESERVED.md")


def test_today_digest_summarizes_today_with_deliverable_excerpt_and_gate_state(
    client, kanban_home
):
    now = int(time.time())
    today = time.localtime(now)
    day_start = int(time.mktime(today[:3] + (0, 0, 0) + today[6:]))
    today_end = max(day_start + 1, now - 60)
    conn = kb.connect()
    try:
        useful_task = kb.create_task(
            conn, title="Ship useful dashboard slice", assignee="coder"
        )
        old_task = kb.create_task(conn, title="Yesterday result", assignee="coder")
        run_id = _insert_completed_run(
            conn,
            task_id=useful_task,
            title="Ship useful dashboard slice",
            started_at=today_end - 120,
            ended_at=today_end,
            summary="S4 complete — digest now answers what arrived today",
            metadata={
                "verdict": "APPROVED",
                "gate_output_excerpt": "web vitest -> 12 passed",
                "receipt_path": "/home/piet/vault/03-Agents/Hermes/receipts/s4.md",
            },
        )
        _append_claimed_event(conn, task_id=useful_task, run_id=run_id)
        _insert_completed_run(
            conn,
            task_id=old_task,
            title="Yesterday result",
            started_at=day_start - 120,
            ended_at=day_start - 60,
            summary="old result should not be in today's digest",
            metadata={},
        )
        conn.commit()
    finally:
        conn.close()

    root = kanban_home / "reports" / "by-task" / useful_task
    root.mkdir(parents=True)
    (root / "RESULT.md").write_text(
        "# Human result\n\nOperator-facing deliverable text for today's useful outcome.\nSecond paragraph.",
        encoding="utf-8",
    )

    r = client.get("/api/plugins/kanban/runs/today-digest")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["schema"] == "kanban-today-digest-v1"
    assert data["count"] == 1
    assert data["day_start"] <= today_end
    item = data["items"][0]
    assert item["task_id"] == useful_task
    assert item["run_id"] == run_id
    assert item["task_summary"] == "S4 complete — digest now answers what arrived today"
    assert item["deliverable"]["relative_path"] == "RESULT.md"
    assert item["deliverable"]["url"].endswith(
        f"/tasks/{useful_task}/deliverables/RESULT.md"
    )
    assert "Operator-facing deliverable text" in item["deliverable_excerpt"]
    assert item["verification_state"] == "approved"
    assert item["verdict_label"] == "Verified: APPROVED"
    assert item["gate_evidence"] == ["web vitest -> 12 passed"]


def test_patch_status_done_with_summary_and_metadata(client):
    """PATCH /tasks/:id with status=done + summary + metadata must
    reach complete_task, so the dashboard has CLI parity."""
    # Create + claim.
    r = client.post(
        "/api/plugins/kanban/tasks", json={"title": "x", "assignee": "worker"}
    )
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
    finally:
        conn.close()

    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={
            "status": "done",
            "summary": "shipped the thing",
            "metadata": {"changed_files": ["a.py", "b.py"], "tests_run": 7},
        },
    )
    assert r.status_code == 200, r.text

    # The run must have the summary + metadata attached.
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, tid)
        assert run.outcome == "completed"
        assert run.summary == "shipped the thing"
        md = {k: v for k, v in run.metadata.items() if k != "cost"}
        assert md == {"changed_files": ["a.py", "b.py"], "tests_run": 7}
    finally:
        conn.close()


def test_patch_status_done_without_summary_still_works(client):
    """Back-compat: PATCH without the new fields still completes."""
    r = client.post(
        "/api/plugins/kanban/tasks", json={"title": "y", "assignee": "worker"}
    )
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"status": "done", "result": "legacy shape"},
    )
    assert r.status_code == 200, r.text
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, tid)
        assert run.outcome == "completed"
        assert run.summary == "legacy shape"  # falls back to result
    finally:
        conn.close()


def test_patch_status_archive_closes_running_run(client):
    """PATCH to archived while running must close the in-flight run."""
    r = client.post(
        "/api/plugins/kanban/tasks", json={"title": "z", "assignee": "worker"}
    )
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        open_run = kb.latest_run(conn, tid)
        assert open_run.ended_at is None
    finally:
        conn.close()
    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"status": "archived"},
    )
    assert r.status_code == 200, r.text
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "archived"
        assert task.current_run_id is None
        assert kb.latest_run(conn, tid).outcome == "reclaimed"
    finally:
        conn.close()


def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post(
        "/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"}
    )
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------


def test_create_task_with_skills_roundtrips(client):
    """POST /tasks accepts `skills: [...]`, GET /tasks/:id returns it."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "translate docs",
            "assignee": "linguist",
            "skills": ["translation", "github-code-review"],
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["skills"] == ["translation", "github-code-review"]

    # Fetch via GET /tasks/:id as the drawer does.
    got = client.get(f"/api/plugins/kanban/tasks/{task['id']}").json()
    assert got["task"]["skills"] == ["translation", "github-code-review"]


def test_create_task_without_skills_defaults_to_empty_list(client):
    """_task_dict serializes Task.skills=None as [] so the drawer can
    always .length check without guarding against null."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "no skills", "assignee": "x"},
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    # Task.skills is None in-memory; _task_dict serializes via
    # dataclasses.asdict which keeps it None. The drawer's
    # `t.skills && t.skills.length > 0` guard handles both null and [].
    assert task.get("skills") in (None, [])


def test_create_task_with_toolset_name_in_skills_is_rejected(client):
    """POST /tasks fails fast when callers confuse toolsets with skills."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "bad skills payload",
            "assignee": "linguist",
            "skills": ["web"],
        },
    )
    assert r.status_code == 400, r.text
    assert "toolset name" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------


def test_create_task_includes_warning_when_no_dispatcher(client, monkeypatch):
    """ready+assigned task + no gateway -> response has `warning` field
    so the dashboard UI can surface a banner."""
    # Force the dispatcher probe to report "not running".
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence",
        lambda: (False, "No gateway is running — start `hermes gateway start`."),
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "warn-me", "assignee": "worker"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("warning")
    assert "gateway" in data["warning"].lower()


def test_create_task_no_warning_when_dispatcher_up(client, monkeypatch):
    """Dispatcher running -> no `warning` field in the response."""
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence",
        lambda: (True, ""),
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "silent", "assignee": "worker"},
    )
    assert r.status_code == 200
    assert "warning" not in r.json() or not r.json()["warning"]


def test_create_task_no_warning_on_triage(client, monkeypatch):
    """Triage tasks never get the warning (they can't be dispatched
    anyway until promoted)."""
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence",
        lambda: (False, "oh no"),
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "triage-task", "assignee": "worker", "triage": True},
    )
    assert r.status_code == 200
    assert "warning" not in r.json() or not r.json()["warning"]


# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------


_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}


def test_board_endpoint_survives_task_age_exception(client, monkeypatch):
    """If task_age raises for any reason, GET /board must NOT 500.

    Pre-fix behavior (without the try/except in _task_dict): a single corrupt
    row turned the entire board response into a 500. The fallback dict lets
    the dashboard render every other card normally.
    """
    create = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "doomed", "assignee": "alice"},
    )
    assert create.status_code == 200, create.text

    # Force task_age to raise an exception type _safe_int does NOT handle —
    # simulates a future regression where someone re-introduces an unguarded
    # operation in task_age. ValueError on '%s' would be absorbed by _safe_int
    # and never reach the outer try/except, so it would not exercise the
    # contract this test pins.
    def _boom(_task):
        raise RuntimeError("simulated future task_age bug")

    monkeypatch.setattr("hermes_cli.kanban_db.task_age", _boom)

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text

    payload = r.json()
    # /board returns columns as a list of {name, tasks} — not a dict — so
    # flatten across all columns to find our seeded task.
    tasks = [t for col in payload["columns"] for t in col["tasks"]]
    assert len(tasks) == 1, f"expected exactly the seeded task, got {tasks!r}"
    # Strict equality: the literal fallback dict from plugin_api._task_dict
    # is the published contract the dashboard UI relies on. Key renames or
    # silent additions should fail this test on purpose.
    assert tasks[0]["age"] == _FALLBACK_AGE


def test_single_task_endpoint_survives_task_age_exception(client, monkeypatch):
    """GET /tasks/:id also calls _task_dict — same fallback should kick in.

    This is the "drawer view" path: the user clicks one card and we serialize
    just that task. A corrupt timestamp on a single task should not block the
    user from opening its drawer.
    """
    create = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "drawer-target", "assignee": "bob"},
    )
    task_id = create.json()["task"]["id"]

    def _boom(_task):
        raise RuntimeError("simulated future task_age bug")

    monkeypatch.setattr("hermes_cli.kanban_db.task_age", _boom)

    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200, r.text
    assert r.json()["task"]["age"] == _FALLBACK_AGE


def test_create_task_probe_error_does_not_break_create(client, monkeypatch):
    """Probe failure must never break task creation."""

    def _raise():
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence",
        _raise,
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "resilient", "assignee": "worker"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["title"] == "resilient"


# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


def test_home_channels_no_task_id_all_unsubscribed(client, with_home_channels):
    """Without task_id, every entry's subscribed=false (UI "no task" state)."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    assert all(not h["subscribed"] for h in r.json()["home_channels"])


def test_home_subscribe_creates_notify_sub_row(client, with_home_channels):
    """POST .../home-subscribe/telegram writes a kanban_notify_subs row
    keyed to the telegram home's (chat_id, thread_id)."""
    from hermes_cli import kanban_db as kb

    # notify_home=False: isolate the explicit home-subscribe endpoint from the
    # FU-3 subscribe-on-create default (which would pre-seed telegram+discord).
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "notify_home": False},
    ).json()["task"]

    r = client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, t["id"])
    finally:
        conn.close()
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "1234567"
    assert subs[0]["thread_id"] == "42"
    assert subs[0]["notifier_profile"] == "default"


def test_home_subscribe_flips_subscribed_flag_in_subsequent_get(
    client, with_home_channels
):
    """After subscribe, the GET endpoint reports subscribed=true for that
    platform and false for the others."""
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "notify_home": False},
    ).json()["task"]
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")

    r = client.get(f"/api/plugins/kanban/home-channels?task_id={t['id']}")
    flags = {h["platform"]: h["subscribed"] for h in r.json()["home_channels"]}
    assert flags == {"telegram": True, "discord": False}


def test_home_subscribe_is_idempotent(client, with_home_channels):
    """Re-subscribing keeps a single row at the DB layer."""
    from hermes_cli import kanban_db as kb

    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "notify_home": False},
    ).json()["task"]
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, t["id"])) == 1
    finally:
        conn.close()


def test_home_subscribe_backfills_owner_on_legacy_row(client, with_home_channels):
    """Re-subscribing should backfill notifier ownership on ownerless rows."""
    from hermes_cli import kanban_db as kb

    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "notify_home": False},
    ).json()["task"]

    conn = kb.connect()
    try:
        kb.add_notify_sub(
            conn,
            task_id=t["id"],
            platform="telegram",
            chat_id="1234567",
            thread_id="42",
        )
    finally:
        conn.close()

    r = client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    assert r.status_code == 200

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, t["id"])
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["notifier_profile"] == "default"


def test_home_subscribe_unknown_platform_returns_404(client, with_home_channels):
    """Platforms without a home configured (slack in the fixture) return 404."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/slack")
    assert r.status_code == 404
    assert "slack" in r.json()["detail"]


def test_home_subscribe_unknown_task_returns_404(client, with_home_channels):
    r = client.post("/api/plugins/kanban/tasks/t_nonexistent/home-subscribe/telegram")
    assert r.status_code == 404


def test_home_unsubscribe_removes_notify_sub_row(client, with_home_channels):
    """DELETE .../home-subscribe/telegram removes the matching row."""
    from hermes_cli import kanban_db as kb

    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "notify_home": False},
    ).json()["task"]
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    assert r.status_code == 200

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, t["id"]) == []
    finally:
        conn.close()


def test_home_subscribe_multiple_platforms_independent(client, with_home_channels):
    """Subscribing on telegram does not affect discord and vice versa."""
    from hermes_cli import kanban_db as kb

    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "notify_home": False},
    ).json()["task"]

    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/discord")

    conn = kb.connect()
    try:
        subs = {s["platform"]: s for s in kb.list_notify_subs(conn, t["id"])}
    finally:
        conn.close()
    assert set(subs) == {"telegram", "discord"}

    # Unsubscribe telegram only.
    client.delete(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    conn = kb.connect()
    try:
        subs = {s["platform"]: s for s in kb.list_notify_subs(conn, t["id"])}
    finally:
        conn.close()
    assert set(subs) == {"discord"}


def test_home_channels_empty_when_no_homes_configured(client, monkeypatch):
    """Zero platforms with a home -> empty list (UI hides the section)."""
    # No BOT_TOKEN env vars set → load_gateway_config().platforms is empty.
    for var in [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_HOME_CHANNEL",
        "DISCORD_BOT_TOKEN",
        "DISCORD_HOME_CHANNEL",
        "SLACK_BOT_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    assert r.json()["home_channels"] == []


# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------


def test_board_surfaces_warnings_field_for_hallucinated_completions(client):
    """Tasks with a pending completion_blocked_hallucination event surface
    a ``warnings`` object on the /board payload so the UI can badge
    them without fetching per-task events. The warnings summary is
    keyed by diagnostic kind (``hallucinated_cards``) rather than the
    raw event kind — see hermes_cli.kanban_diagnostics for the rule
    that produces it.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")

        import pytest as _pytest

        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn,
                parent,
                summary="claimed phantom",
                created_cards=[real, "t_deadbeefcafe"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    tasks = [t for col in data["columns"] for t in col["tasks"]]
    parent_dict = next(t for t in tasks if t["title"] == "parent")
    assert parent_dict.get("warnings") is not None
    w = parent_dict["warnings"]
    assert w["count"] >= 1
    assert "hallucinated_cards" in w["kinds"]
    assert w["highest_severity"] == "error"
    # Full diagnostic list also on the payload for drawer rendering.
    assert parent_dict.get("diagnostics") is not None
    assert parent_dict["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert "t_deadbeefcafe" in parent_dict["diagnostics"][0]["data"]["phantom_ids"]


def test_board_warnings_cleared_after_clean_completion(client):
    """A completed or edited event after a hallucination event clears
    the warning badge — we don't mark tasks permanently."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")

        import pytest as _pytest

        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn,
                parent,
                summary="first attempt phantom",
                created_cards=[real, "t_phantom11"],
            )

        # Second attempt drops the bad id — succeeds.
        ok = kb.complete_task(
            conn,
            parent,
            summary="retry without phantom",
            created_cards=[real],
        )
        assert ok is True
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board", params={"include_archived": True})
    assert r.status_code == 200
    data = r.json()
    tasks = [t for col in data["columns"] for t in col["tasks"]]
    parent_dict = next(t for t in tasks if t["title"] == "parent")
    # The clean completion wiped the warning.
    assert parent_dict.get("warnings") is None


def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets

    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()


def test_reclaim_endpoint_409_for_non_running_task(client):
    """Reclaiming a task that's already ready returns 409."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="ready", assignee="x")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# R1 (P1-repair-action): POST /tasks/<id>/repair — operator nachschließen des
# fehlenden kanban_complete-Schritts für ein deliverable_posted_not_completed.
# ---------------------------------------------------------------------------
def _seed_deliverable_miss(monkeypatch, *, title="repair me"):
    """Drive a task into the blocked ``deliverable_posted_not_completed`` state
    (worker posted a deliverable, exited rc=0 without ``kanban_complete``)."""
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="coder")
        kb.claim_task(conn, tid)
        kb.add_comment(
            conn,
            tid,
            "coder",
            (
                "# Deliverable: " + title + "\n\n"
                "The work is complete and mapped to the requested objective. "
                "Evidence includes the final section list, validation notes, "
                "and remaining risk. " + "x" * 120
            ),
        )
        pid = 525252
        kb._set_worker_pid(conn, tid, pid)
        kb._record_worker_exit(pid, 0)
        kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()
    return tid


def test_repair_endpoint_requires_confirm(client, monkeypatch):
    """Without confirm:true the endpoint refuses (ok:false at HTTP 200) and
    performs no mutation — same guard contract as /workers/<id>/action."""
    tid = _seed_deliverable_miss(monkeypatch, title="confirm-guard")

    r = client.post(f"/api/plugins/kanban/tasks/{tid}/repair", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "confirm" in body["detail"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"  # untouched
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "deliverable_protocol_repaired" not in kinds
    finally:
        conn.close()


def test_repair_endpoint_closes_deliverable_miss(client, monkeypatch):
    """With confirm:true the missing kanban_complete is closed: blocked→done,
    a deliverable_protocol_repaired event is emitted, ready is recomputed."""
    tid = _seed_deliverable_miss(monkeypatch, title="repairable")

    r = client.post(
        f"/api/plugins/kanban/tasks/{tid}/repair",
        json={"confirm": True, "actor": "operator-test"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == tid

    conn = kb.connect()
    try:
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        repair_events = [
            e
            for e in kb.list_events(conn, tid)
            if e.kind == "deliverable_protocol_repaired"
        ]
        assert repair_events
        assert repair_events[-1].payload["actor"] == "operator-test"
        # No review verdict written by a protocol repair.
        verdicts = conn.execute(
            "SELECT verdict FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchall()
        assert all(row["verdict"] is None for row in verdicts)
    finally:
        conn.close()


def test_repair_endpoint_guard_when_nothing_to_repair(client):
    """A task with no repairable deliverable_posted_not_completed evidence is a
    soft refusal (ok:false at HTTP 200), not a crash."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="nothing-to-repair", assignee="coder")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{tid}/repair",
        json={"confirm": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False


def test_decision_queue_surfaces_deliverable_posted_not_completed(client, monkeypatch):
    """A blocked deliverable-miss surfaces in the decision queue under the
    dedicated kind so the dashboard can wire the repair button."""
    tid = _seed_deliverable_miss(monkeypatch, title="surface-me")

    r = client.get("/api/plugins/kanban/decision-queue")
    assert r.status_code == 200, r.text
    rows = r.json()["decisions"]
    match = next((d for d in rows if d["task_id"] == tid), None)
    assert match is not None
    assert match["kind"] == "deliverable_posted_not_completed"


def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?",
            (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()


def test_reassign_endpoint_409_on_running_without_reclaim(client):
    """Reassigning a running task without reclaim_first returns 409."""
    import secrets

    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="orig")
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=? WHERE id=?",
            (secrets.token_hex(4), t),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "new", "reclaim_first": False},
    )
    assert r.status_code == 409


def test_reassign_endpoint_with_reclaim_first_succeeds_on_running(client):
    """With reclaim_first=true, a running task is reclaimed+reassigned in
    one call."""
    import secrets

    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="orig")
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 1234, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, int(time.time()) + 3600, 1234, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "new", "reclaim_first": True, "reason": "switch"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "new"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, assignee FROM tasks WHERE id=?",
            (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["assignee"] == "new"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------


def test_diagnostics_endpoint_empty_for_clean_board(client):
    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["diagnostics"] == []


def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest

        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn,
                parent,
                summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]


def test_diagnostics_endpoint_surfaces_reviewer_role_tool_mismatch(client):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Reviewer gates",
            body="Reviewer: run pytest and git diff --check in the repo.",
            assignee="reviewer",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == task_id
    assert row["diagnostics"][0]["kind"] == "reviewer_role_tool_mismatch"
    assert row["diagnostics"][0]["severity"] == "warning"


def test_diagnostics_endpoint_severity_filter(client):
    """Severity filter is at-or-above: warning includes warning+error+critical,
    error includes error+critical, critical is exact (no higher level)."""
    conn = kb.connect()
    try:
        # A warning-severity diagnostic (prose phantom) on one task.
        # Phantom id must be valid hex — the prose scanner regex
        # requires ``t_[a-f0-9]{8,}``.
        p1 = kb.create_task(conn, title="prose", assignee="a")
        kb.complete_task(conn, p1, summary="mentioned t_deadbeef1234")
        # An error-severity diagnostic (spawn failures) on another.
        # Keep this below critical severity (failure_threshold * 2).
        p2 = kb.create_task(conn, title="spawn", assignee="b")
        conn.execute(
            "UPDATE tasks SET consecutive_failures=2, last_failure_error='x' WHERE id=?",
            (p2,),
        )
        conn.commit()
    finally:
        conn.close()

    # warning filter is at-or-above → both the warning AND the error pass.
    r = client.get("/api/plugins/kanban/diagnostics?severity=warning")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    task_ids = {row["task_id"] for row in data["diagnostics"]}
    assert task_ids == {p1, p2}

    # error filter is at-or-above → only the error passes (warning is below).
    r = client.get("/api/plugins/kanban/diagnostics?severity=error")
    data = r.json()
    assert data["count"] == 1
    assert data["diagnostics"][0]["task_id"] == p2


def test_board_exposes_diagnostics_list_and_summary(client):
    """/board should attach both the full diagnostics list AND the
    compact warnings summary (with highest_severity) on each task
    that has any diagnostic.
    """
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="crashy", assignee="worker")
        # Simulate 2 consecutive crashes -> repeated_crashes error diag
        for i in range(2):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, outcome, started_at, "
                "ended_at, error) VALUES (?, 'crashed', 'crashed', ?, ?, ?)",
                (t, int(time.time()) - 100, int(time.time()) - 50, "OOM"),
            )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    data = r.json()
    tasks = [x for col in data["columns"] for x in col["tasks"]]
    task_dict = next(x for x in tasks if x["title"] == "crashy")
    assert task_dict["warnings"] is not None
    assert task_dict["warnings"]["highest_severity"] == "error"
    assert task_dict["diagnostics"][0]["kind"] == "repeated_crashes"


def test_board_exposes_new_warning_diagnostic_summary(client):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Reviewer gates",
            body="Reviewer: run pytest in the repo.",
            assignee="reviewer",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    tasks = [x for col in data["columns"] for x in col["tasks"]]
    task_dict = next(x for x in tasks if x["id"] == task_id)
    assert task_dict["warnings"] is not None
    assert task_dict["warnings"]["highest_severity"] == "warning"
    assert task_dict["warnings"]["kinds"]["reviewer_role_tool_mismatch"] == 1
    assert task_dict["diagnostics"][0]["kind"] == "reviewer_role_tool_mismatch"


# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------


def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(return_value=resp)
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda *a, **kw: (fake_client, model),
    )
    return fake_client


def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps({"title": "Polished", "body": "**Goal**\nDo the thing."}),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")


def test_specify_non_triage_returns_ok_false_not_http_error(client, monkeypatch):
    """The endpoint intentionally returns ``{ok: false, reason: ...}`` for
    "task not in triage" rather than a 4xx — the dashboard renders the
    reason inline so the user can fix it without a page reload."""
    # Create a normal (ready) task — not in triage.
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    _patch_specifier_response(monkeypatch, content="unused")

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "not in triage" in body["reason"]


def test_specify_no_aux_client_surfaces_reason(client, monkeypatch):
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "rough", "triage": True},
    ).json()["task"]

    # Simulate "no auxiliary client configured".
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda *a, **kw: (None, ""),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "auxiliary client" in body["reason"]

    # Task must stay in triage — nothing was touched.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] == "triage"


def test_board_endpoint_accepts_explicit_board_default_param(client):
    """GET /board?board=default must not fall through to env/current-file resolution.

    The dashboard always sends ``?board=<slug>`` (including ``board=default``)
    so that the server-side ``current`` file can never override the dashboard's
    selected board.  This test asserts the endpoint accepts the parameter and
    returns the default board without falling back to environment variable or
    current-file resolution.
    Regression: #21819.
    """
    # Create a task on the default board.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "on-default-board"},
    ).json()["task"]
    assert t["status"] == "ready"

    # Request with explicit board=default — must succeed and include the task.
    r = client.get("/api/plugins/kanban/board?board=default")
    assert r.status_code == 200
    data = r.json()
    ready = next((c for c in data["columns"] if c["name"] == "ready"), None)
    assert ready is not None, "no 'ready' column in default board response"
    task_ids = [task["id"] for task in ready["tasks"]]
    assert t["id"] in task_ids, (
        f"task {t['id']} not found in ready column of default board "
        f"(got tasks: {task_ids}). The board=default param was likely ignored."
    )


def test_dashboard_requests_default_board_explicitly():
    """Dashboard REST calls must include board=default instead of relying on server current board."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert "SDK.fetchJSON(withBoard(`${API}/config`, board))" in dist
    assert "SDK.fetchJSON(withBoard(`${API}/boards`, board))" in dist
    assert "}, [loadBoardList, switchBoard, board]);" in dist


def test_dashboard_search_includes_body_and_result():
    """Client-side search must match body, result, latest_summary, and summary
    so full card contents are findable."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert 't.body || ""' in dist
    assert 't.result || ""' in dist
    assert 't.latest_summary || ""' in dist


def test_dashboard_bulk_actions_include_reclaim_first():
    """Bulk action bar must expose reclaim_first checkbox and expanded status buttons."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert "reclaim_first: reclaimFirst" in dist
    assert "hermes-kanban-bulk-reclaim-first" in dist
    assert '"→ todo"' in dist
    assert '"Block"' in dist
    assert '"Unblock"' in dist


def test_dashboard_shift_click_range_selection_exists():
    """Shift-click must trigger range selection via toggleRange."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert "function toggleRange" in dist or "const toggleRange =" in dist
    assert "props.toggleRange(t.id)" in dist or "props.toggleRange" in dist
    assert "e.shiftKey" in dist


def test_dashboard_multi_move_bulk_exists():
    """Dragging a selected card with other selections must use /tasks/bulk."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert "onMoveSelected" in dist
    assert "props.onMoveSelected" in dist
    assert "`${API}/tasks/bulk`" in dist


def test_dashboard_failed_card_highlight_class_exists():
    """Partial bulk failures must highlight failing cards."""
    repo_root = Path(__file__).resolve().parents[2]
    js = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()
    css = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "style.css"
    ).read_text()

    assert "hermes-kanban-card--failed" in js
    assert "hermes-kanban-card--failed" in css
    assert "failedIds" in js


# ---------------------------------------------------------------------------
# FU-3: subscribe-on-create routes dashboard-created tasks to home channels
# ---------------------------------------------------------------------------

_FAKE_HOME = [
    {"platform": "telegram", "chat_id": "home-1", "thread_id": "", "name": "Home"}
]


def test_create_task_subscribes_to_home_channel(client, monkeypatch):
    """A dashboard-created task is auto-subscribed to every configured home
    channel, so its terminal state (and its decompose children's, via H1
    inheritance) reaches the home channel without a manual notify-subscribe.
    """
    import gateway.config as gwc

    monkeypatch.setattr(gwc, "configured_home_channels", lambda: list(_FAKE_HOME))

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "ship a feature"},
    ).json()["task"]

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task["id"])
    finally:
        conn.close()
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "home-1"


def test_create_task_notify_home_false_skips_subscription(client, monkeypatch):
    """notify_home=False opts out of the home subscription (bulk/scripted use)."""
    import gateway.config as gwc

    monkeypatch.setattr(gwc, "configured_home_channels", lambda: list(_FAKE_HOME))

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "no ping please", "notify_home": False},
    ).json()["task"]

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task["id"])
    finally:
        conn.close()
    assert subs == []


def test_create_task_no_home_channels_is_noop(client, monkeypatch):
    """No configured home channel -> create still succeeds, just no sub."""
    import gateway.config as gwc

    monkeypatch.setattr(gwc, "configured_home_channels", lambda: [])

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "homeless"},
    ).json()["task"]

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task["id"])
    finally:
        conn.close()
    assert subs == []


# ---------------------------------------------------------------------------
# GET /runs/recent-results - completed worker handoff visibility
# ---------------------------------------------------------------------------


def _insert_completed_run(
    conn,
    *,
    task_id,
    title,
    started_at,
    ended_at,
    outcome="completed",
    summary="",
    metadata=None,
    profile: str | None = "coder",
):
    conn.execute("UPDATE tasks SET title=? WHERE id=?", (title, task_id))
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, summary, metadata) "
        "VALUES (?, ?, 'done', ?, ?, ?, ?, ?)",
        (
            task_id,
            profile,
            outcome,
            started_at,
            ended_at,
            summary,
            json.dumps(metadata or {}),
        ),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _append_claimed_event(conn, *, task_id, run_id, payload=None):
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'claimed', ?, ?)",
        (task_id, run_id, json.dumps(payload or {"run_id": run_id}), int(time.time())),
    )


def test_recent_results_defaults_to_completed_newest_first_and_normalizes_metadata(
    client,
):
    now = int(time.time())
    conn = kb.connect()
    try:
        older_task = kb.create_task(conn, title="older", assignee="coder")
        newer_task = kb.create_task(conn, title="newer", assignee="research")
        blocked_task = kb.create_task(conn, title="blocked", assignee="critic")
        _insert_completed_run(
            conn,
            task_id=older_task,
            title="Ship receipt artifact",
            started_at=now - 500,
            ended_at=now - 400,
            summary="First line\nSecond line with details",
            metadata={
                "artifact": "/tmp/a.txt",
                "tests_run": ["pytest x"],
                "residual_risk": "needs operator review",
            },
        )
        newer_run = _insert_completed_run(
            conn,
            task_id=newer_task,
            title="Verify changed files",
            started_at=now - 120,
            ended_at=now - 60,
            summary="Verified worker output",
            metadata={
                "next_actions": ["open board drawer"],
                "artifacts": ["/tmp/b.txt"],
                "receipt_path": "/tmp/receipt.md",
                "verification_evidence": ["curl ok"],
                "changed_files": ["web/src/x.ts"],
            },
            profile="research",
        )
        _insert_completed_run(
            conn,
            task_id=blocked_task,
            title="Blocked task",
            started_at=now - 40,
            ended_at=now - 20,
            outcome="blocked",
            summary="blocked summary",
            metadata={"next_actions": ["not in default"]},
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/runs/recent-results")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    assert [row["task_title"] for row in data["results"]] == [
        "Verify changed files",
        "Ship receipt artifact",
    ]
    first = data["results"][0]
    assert first["run_id"] == newer_run
    assert first["followups"] == ["open board drawer"]
    assert first["artifacts"] == ["/tmp/b.txt", "/tmp/receipt.md"]
    assert first["verification"] == ["curl ok", "web/src/x.ts"]
    assert first["summary_preview"] == "Verified worker output"
    second = data["results"][1]
    assert second["followups"] == ["needs operator review"]
    assert second["artifacts"] == ["/tmp/a.txt"]
    assert second["verification"] == ["pytest x"]


def test_recent_results_surfaces_verifier_verdict_evidence_and_ungated_state(client):
    now = int(time.time())
    conn = kb.connect()
    try:
        approved_task = kb.create_task(conn, title="approved", assignee="coder")
        ungated_task = kb.create_task(conn, title="ungated", assignee="research")
        rejected_task = kb.create_task(conn, title="rejected", assignee="coder")
        legacy_task = kb.create_task(conn, title="legacy", assignee="legacy")
        _insert_completed_run(
            conn,
            task_id=approved_task,
            title="Verifier approved task",
            started_at=now - 120,
            ended_at=now - 60,
            summary="APPROVED — checked real output",
            metadata={
                "verdict": "APPROVED",
                "gate_output_excerpt": "python3 check.py -> stdout: CHECK OK",
                "verification_evidence": ["pytest tests/foo.py -> 1 passed"],
            },
            profile="verifier",
        )
        _insert_completed_run(
            conn,
            task_id=ungated_task,
            title="Direct ungated task",
            started_at=now - 50,
            ended_at=now - 20,
            summary="completed without reviewer",
            metadata={"changed_files": ["notes.md"]},
            profile="research",
        )
        _insert_completed_run(
            conn,
            task_id=rejected_task,
            title="Verifier rejected task",
            started_at=now - 80,
            ended_at=now - 30,
            summary="REQUEST_CHANGES — tests failed",
            metadata={
                "verdict": "REQUEST_CHANGES",
                "gate_output_excerpt": "pytest -> failed",
            },
            profile="verifier",
        )
        _insert_completed_run(
            conn,
            task_id=legacy_task,
            title="Legacy unknown task",
            started_at=now - 70,
            ended_at=now - 10,
            summary="old completion without profile or verifier metadata",
            metadata={},
            profile=None,
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/runs/recent-results")
    assert r.status_code == 200, r.text
    rows = {row["task_title"]: row for row in r.json()["results"]}
    approved = rows["Verifier approved task"]
    assert approved["verification_state"] == "approved"
    assert approved["verifier_verdict"] == "APPROVED"
    assert approved["verifier_evidence"] == [
        "python3 check.py -> stdout: CHECK OK",
        "pytest tests/foo.py -> 1 passed",
    ]
    assert approved["result_quality"] == {
        "state": "verifier_approved",
        "label": "Verifier-approved",
        "tone": "emerald",
        "description": "Independent verifier gate passed.",
    }
    ungated = rows["Direct ungated task"]
    assert ungated["verification_state"] == "ungated"
    assert ungated["verifier_verdict"] is None
    assert ungated["verifier_evidence"] == []
    assert ungated["result_quality"]["state"] == "ungated"
    assert ungated["result_quality"]["label"] == "Ungated"
    rejected = rows["Verifier rejected task"]
    assert rejected["verification_state"] == "request_changes"
    assert rejected["result_quality"]["state"] == "rejected_needs_work"
    assert rejected["result_quality"]["label"] == "Rejected / needs work"
    legacy = rows["Legacy unknown task"]
    assert legacy["verification_state"] == "ungated"
    assert legacy["result_quality"] == {
        "state": "unknown_legacy",
        "label": "Unknown legacy",
        "tone": "zinc",
        "description": "Legacy run has no verifier metadata or profile lineage.",
    }


def test_recent_results_exposes_run_lineage_without_profile_fallbacks(client):
    now = int(time.time())
    conn = kb.connect()
    try:
        coder_task = kb.create_task(conn, title="coder", assignee="coder")
        verifier_task = kb.create_task(conn, title="verifier", assignee="coder")
        legacy_task = kb.create_task(conn, title="legacy", assignee="coder")
        coder_run = _insert_completed_run(
            conn,
            task_id=coder_task,
            title="Implementation run",
            started_at=now - 300,
            ended_at=now - 240,
            summary="implementation done",
            metadata={},
            profile="coder",
        )
        _append_claimed_event(
            conn, task_id=coder_task, run_id=coder_run, payload={"run_id": coder_run}
        )
        verifier_run = _insert_completed_run(
            conn,
            task_id=verifier_task,
            title="Verifier run",
            started_at=now - 200,
            ended_at=now - 140,
            summary="APPROVED — tests passed",
            metadata={"verdict": "APPROVED"},
            # Historical review rows were persisted with the task assignee
            # even though the dispatcher spawned the verifier profile.
            profile="coder",
        )
        _append_claimed_event(
            conn,
            task_id=verifier_task,
            run_id=verifier_run,
            payload={"run_id": verifier_run, "source_status": "review"},
        )
        _insert_completed_run(
            conn,
            task_id=legacy_task,
            title="Legacy run",
            started_at=now - 100,
            ended_at=now - 40,
            summary="old row without claimed event",
            metadata={},
            profile=None,
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/runs/recent-results?limit=10")
    assert r.status_code == 200, r.text
    rows = {row["task_title"]: row for row in r.json()["results"]}
    assert rows["Implementation run"]["run_role"] == "implementation"
    assert rows["Implementation run"]["run_role_label"] == "Implementation / coder run"
    assert rows["Implementation run"]["run_role_source"] == "claimed_event"

    assert rows["Verifier run"]["profile"] == "coder"
    assert rows["Verifier run"]["run_role"] == "verification"
    assert rows["Verifier run"]["run_role_label"] == "Verifier / review run"
    assert rows["Verifier run"]["run_role_source"] == "claimed_event"

    assert rows["Legacy run"]["profile"] is None
    assert rows["Legacy run"]["run_role"] == "legacy_unknown"
    assert rows["Legacy run"]["run_role_label"] == "Unknown / legacy run"
    assert rows["Legacy run"]["run_role_source"] == "missing_claim_event"


def test_task_detail_runs_include_lineage_for_coder_verifier_and_legacy(client):
    now = int(time.time())
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="mixed lineage", assignee="coder")
        coder_run = _insert_completed_run(
            conn,
            task_id=task_id,
            title="mixed lineage",
            started_at=now - 300,
            ended_at=now - 240,
            summary="coder summary",
            metadata={},
            profile="coder",
        )
        verifier_run = _insert_completed_run(
            conn,
            task_id=task_id,
            title="mixed lineage",
            started_at=now - 200,
            ended_at=now - 140,
            summary="verifier summary",
            metadata={},
            profile="coder",
        )
        legacy_run = _insert_completed_run(
            conn,
            task_id=task_id,
            title="mixed lineage",
            started_at=now - 100,
            ended_at=now - 40,
            summary="legacy summary",
            metadata={},
            profile="coder",
        )
        _append_claimed_event(
            conn, task_id=task_id, run_id=coder_run, payload={"run_id": coder_run}
        )
        _append_claimed_event(
            conn,
            task_id=task_id,
            run_id=verifier_run,
            payload={"run_id": verifier_run, "source_status": "review"},
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200, r.text
    runs = {row["id"]: row for row in r.json()["runs"]}
    assert runs[coder_run]["run_role"] == "implementation"
    assert runs[verifier_run]["run_role"] == "verification"
    assert runs[legacy_run]["run_role"] == "legacy_unknown"


def test_review_verdicts_surfaces_review_tasks_with_request_changes_evidence(client):
    now = int(time.time())
    conn = kb.connect()
    try:
        review_task = kb.create_task(conn, title="Review me", assignee="coder")
        done_task = kb.create_task(conn, title="Done ignored", assignee="coder")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review_task,))
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (done_task,))
        _insert_completed_run(
            conn,
            task_id=review_task,
            title="Review me",
            started_at=now - 90,
            ended_at=now - 30,
            summary="REQUEST_CHANGES — pytest failed",
            metadata={
                "verdict": "REQUEST_CHANGES",
                "verification_evidence": [
                    "pytest tests/foo.py -> stdout: FAILED test_add"
                ],
            },
            profile="verifier",
        )
        _insert_completed_run(
            conn,
            task_id=done_task,
            title="Done ignored",
            started_at=now - 80,
            ended_at=now - 20,
            summary="APPROVED",
            metadata={"verdict": "APPROVED"},
            profile="verifier",
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/tasks/review-verdicts")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    row = data["reviews"][0]
    assert row["task_title"] == "Review me"
    assert row["task_status"] == "review"
    assert row["reviewer_profile"] == "verifier"
    assert row["verifier_verdict"] == "REQUEST_CHANGES"
    assert row["verification_state"] == "request_changes"
    assert row["verifier_evidence"] == [
        "pytest tests/foo.py -> stdout: FAILED test_add"
    ]


def test_review_verdicts_surfaces_active_verifier_run_for_review_claim(client):
    conn = kb.connect()
    try:
        review_task = kb.create_task(conn, title="Review active", assignee="coder")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review_task,))
        claimed = kb.claim_review_task(
            conn, review_task, claimer="test-host:944", reviewer_profile="verifier"
        )
        assert claimed is not None
        run = kb.latest_run(conn, review_task)
        assert run is not None
        run_id = run.id
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/tasks/review-verdicts")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    row = data["reviews"][0]
    assert row["task_id"] == review_task
    assert row["task_status"] == "running"
    assert row["run_id"] == run_id
    assert row["active_verifier"] is True
    assert row["active_run_id"] == run_id
    assert row["review_run_state"] == "active"
    assert row["review_run_source"] == "claimed_event"
    assert row["reviewer_profile"] == "verifier"
    assert row["verification_state"] == "pending"
    assert row["verifier_verdict"] is None


def test_patch_status_done_rejected_from_review_without_review_done_affordance(client):
    conn = kb.connect()
    try:
        review_task = kb.create_task(
            conn, title="Cannot manually finish review", assignee="coder"
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review_task,))
    finally:
        conn.close()

    r = client.patch(
        f"/api/plugins/kanban/tasks/{review_task}", json={"status": "done"}
    )
    assert r.status_code == 409
    assert "not valid" in r.json()["detail"] or "refused" in r.json()["detail"]


def test_recent_results_caps_limit_filters_since_and_truncates_summary(client):
    now = int(time.time())
    conn = kb.connect()
    try:
        old_task = kb.create_task(conn, title="old", assignee="coder")
        new_task = kb.create_task(conn, title="new", assignee="coder")
        _insert_completed_run(
            conn,
            task_id=old_task,
            title="too old",
            started_at=now - 200000,
            ended_at=now - 190000,
            summary="old",
        )
        _insert_completed_run(
            conn,
            task_id=new_task,
            title="large summary",
            started_at=now - 30,
            ended_at=now - 10,
            summary="x" * 9000,
            metadata={"required_verification": ["check"], "suggested_fixes": ["fix"]},
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/runs/recent-results?since_hours=1&limit=999")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["limit"] == 50
    assert data["count"] == 1
    result = data["results"][0]
    assert result["task_title"] == "large summary"
    assert len(result["summary"]) == 8192
    assert len(result["summary_preview"]) == 160
    assert result["followups"] == ["check", "fix"]


# ---------------------------------------------------------------------------
# GET /runs/blocked-completions - hallucination-refusal visibility
# ---------------------------------------------------------------------------


def test_blocked_completions_surfaces_refused_and_advisory_events(client):
    """The endpoint returns both blocked-completion and advisory
    hallucination events (newest first), unifying ``phantom_cards`` /
    ``phantom_refs`` into a single ``phantom`` list and surfacing the
    payload ``summary_preview``."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="Phantom claimer", assignee="critic")
        real = kb.create_task(conn, title="real", assignee="x", created_by="critic")

        # Real complete_task path emits completion_blocked_hallucination with
        # phantom_cards + summary_preview, then raises.
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn,
                parent,
                summary="Erstellte Karte t_deadbeefcafe wie gewuenscht",
                created_cards=[real, "t_deadbeefcafe"],
            )

        # Advisory prose-scan event (completion succeeded, advisory only).
        advisory_task = kb.create_task(
            conn, title="Advisory prose", assignee="research"
        )
        kb._append_event(
            conn,
            advisory_task,
            "suspected_hallucinated_references",
            {"phantom_refs": ["t_cafef00dbabe"], "source": "summary"},
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/runs/blocked-completions")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    assert data["since_hours"] == 48
    kinds = [row["kind"] for row in data["blocked"]]
    assert "completion_blocked_hallucination" in kinds
    assert "suspected_hallucinated_references" in kinds

    blocked_row = next(
        b for b in data["blocked"] if b["kind"] == "completion_blocked_hallucination"
    )
    assert blocked_row["task_title"] == "Phantom claimer"
    assert blocked_row["assignee"] == "critic"
    assert "t_deadbeefcafe" in blocked_row["phantom"]
    assert (
        blocked_row["summary_preview"]
        == "Erstellte Karte t_deadbeefcafe wie gewuenscht"
    )
    assert "event_id" in blocked_row

    advisory_row = next(
        b for b in data["blocked"] if b["kind"] == "suspected_hallucinated_references"
    )
    assert advisory_row["phantom"] == ["t_cafef00dbabe"]
    assert advisory_row["summary_preview"] is None


def test_blocked_completions_surfaces_verifier_request_changes_with_fix_summary(client):
    """Verifier REQUEST_CHANGES runs are shown beside blocked completions
    with quoted failure output and the concrete fix target."""
    now = int(time.time())
    conn = kb.connect()
    try:
        rejected_task = kb.create_task(
            conn, title="Rejected by verifier", assignee="coder"
        )
        approved_task = kb.create_task(conn, title="Approved ignored", assignee="coder")
        non_verifier_task = kb.create_task(
            conn, title="Critic ignored", assignee="critic"
        )

        rejected_run = _insert_completed_run(
            conn,
            task_id=rejected_task,
            title="Rejected by verifier",
            started_at=now - 120,
            ended_at=now - 60,
            outcome="blocked",
            summary="REQUEST_CHANGES — pytest failed; fix add(a, b) to return the sum.",
            metadata={
                "verdict": "REQUEST_CHANGES",
                "gate_output_excerpt": "pytest tests/test_calc.py -> stdout: FAILED test_add",
                "fix_summary": "Fix add(a, b) to return a + b before resubmitting.",
            },
            profile="verifier",
        )
        _append_claimed_event(
            conn,
            task_id=rejected_task,
            run_id=rejected_run,
            payload={"run_id": rejected_run, "source_status": "review"},
        )
        _insert_completed_run(
            conn,
            task_id=approved_task,
            title="Approved ignored",
            started_at=now - 100,
            ended_at=now - 50,
            summary="APPROVED — tests passed",
            metadata={"verdict": "APPROVED"},
            profile="verifier",
        )
        _insert_completed_run(
            conn,
            task_id=non_verifier_task,
            title="Critic ignored",
            started_at=now - 90,
            ended_at=now - 40,
            summary="REQUEST_CHANGES: not a verifier run",
            metadata={"verdict": "REQUEST_CHANGES"},
            profile="critic",
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/runs/blocked-completions")
    assert r.status_code == 200, r.text
    data = r.json()
    row = next(b for b in data["blocked"] if b["kind"] == "verifier_request_changes")
    assert row["task_title"] == "Rejected by verifier"
    assert row["run_id"] == rejected_run
    assert row["reviewer_profile"] == "verifier"
    assert row["verifier_verdict"] == "REQUEST_CHANGES"
    assert row["failure_output"] == [
        "pytest tests/test_calc.py -> stdout: FAILED test_add"
    ]
    assert row["fix_summary"] == "Fix add(a, b) to return a + b before resubmitting."
    assert all(b["task_title"] != "Approved ignored" for b in data["blocked"])
    assert all(b["task_title"] != "Critic ignored" for b in data["blocked"])


def test_blocked_completions_filters_by_since_hours(client):
    """Events older than the since_hours window are excluded."""
    now = int(time.time())
    conn = kb.connect()
    try:
        old_task = kb.create_task(conn, title="old block", assignee="critic")
        kb._append_event(
            conn,
            old_task,
            "completion_blocked_hallucination",
            {"phantom_cards": ["t_oldphantom00"], "summary_preview": "old"},
        )
        # Backdate the event past the window.
        conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='completion_blocked_hallucination'",
            (now - 200000, old_task),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/runs/blocked-completions?since_hours=1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 0
    assert data["since_hours"] == 1


# ---------------------------------------------------------------------------
# Flow capture Phase B — /flow-release + /flow-plan endpoints
# ---------------------------------------------------------------------------


def _setup_gated_root(tenant="flow-capture"):
    """Create a root parked in scheduled with three HELD (scheduled) children
    via the real DB fan-out — no LLM. Returns (root_id, child_ids)."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="gated root", body="a; b; c", triage=True, tenant=tenant
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (root,))
        kb.schedule_task(conn, root, reason="parked")
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="default",
            children=[
                {"title": "a", "body": "a", "assignee": "coder", "parents": []},
                {"title": "b", "body": "b", "assignee": "coder", "parents": []},
                {
                    "title": "c needs a,b",
                    "body": "c",
                    "assignee": "reviewer",
                    "parents": [0, 1],
                },
            ],
            author="user",
            auto_promote=False,
            initial_child_status="scheduled",
            expected_root_status="scheduled",
        )
    return root, child_ids


def test_flow_release_unblocks_scheduled_children_dag_correct(client):
    root, child_ids = _setup_gated_root()
    # Pre: all children held in scheduled.
    with kb.connect() as conn:
        assert all(kb.get_task(conn, c).status == "scheduled" for c in child_ids)

    r = client.post(f"/api/plugins/kanban/tasks/{root}/flow-release")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["released"] == 3, body
    assert set(body["released_ids"]) == set(child_ids)

    with kb.connect() as conn:
        st = {c: kb.get_task(conn, c).status for c in child_ids}
    # Parent-free children -> ready; the dependent child waits in todo.
    assert st[child_ids[0]] == "ready" and st[child_ids[1]] == "ready", st
    assert st[child_ids[2]] == "todo", st

    # Idempotent: a second release finds nothing scheduled.
    r2 = client.post(f"/api/plugins/kanban/tasks/{root}/flow-release")
    assert r2.status_code == 200 and r2.json()["released"] == 0


def test_flow_release_unparks_signed_complete_root_with_children(client):
    root, child_ids = _setup_gated_root()
    assert child_ids is not None
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='scheduled', freigabe='complete' WHERE id=?",
                (root,),
            )

    r = client.post(f"/api/plugins/kanban/tasks/{root}/flow-release")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root_released"] is True
    released_ids = body["released_ids"]
    assert isinstance(released_ids, list)
    assert set(released_ids) == set(child_ids)

    with kb.connect() as conn:
        root_task = kb.get_task(conn, root)
        assert root_task is not None
        child_statuses = {}
        for child in child_ids:
            child_task = kb.get_task(conn, child)
            assert child_task is not None
            child_statuses[child] = child_task.status
        kinds = [event.kind for event in kb.list_events(conn, root)]
    assert root_task.status == "todo"
    assert child_statuses[child_ids[0]] == "ready"
    assert child_statuses[child_ids[1]] == "ready"
    assert child_statuses[child_ids[2]] == "todo"
    assert "flow_gate_root_released" in kinds


def test_flow_gate_proposal_surfaces_risk_cost_lanes_and_timeout(client):
    root, child_ids = _setup_gated_root()

    r = client.get(f"/api/plugins/kanban/tasks/{root}/flow-gate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root_id"] == root
    assert body["held_count"] == 3
    assert [c["id"] for c in body["children"]] == child_ids
    assert all("risk" in c for c in body["children"])
    assert set(body["release_levels"]) == {"merge", "live"}
    assert body["timeout_seconds"] == 1800
    assert body["timeout_at"] is not None
    assert body["cost_estimate"]["estimated_tokens"] > 0
    assert body["cost_estimate"]["estimated_cost_usd"] > 0
    assert body["cost_estimate"]["soft_limit_usd"] == 1.0
    profiles = {p for lane in body["lanes"] for p in lane["profiles"]}
    assert "coder" in profiles and "reviewer" in profiles


def test_flow_release_applies_lane_override_and_records_release_level(client):
    root, child_ids = _setup_gated_root()

    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={
            "assignee_overrides": {child_ids[0]: "reviewer"},
            "release_level": "live",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["released"] == 3
    assert body["release_level"] == "live"
    assert body["assignee_overrides"] == {child_ids[0]: "reviewer"}

    with kb.connect() as conn:
        assert kb.get_task(conn, child_ids[0]).assignee == "reviewer"
        release_events = [
            e for e in kb.list_events(conn, root) if e.kind == "flow_gate_released"
        ]
    assert release_events
    assert release_events[-1].payload["release_level"] == "live"
    assert release_events[-1].payload["released_ids"] == child_ids


# ---------------------------------------------------------------------------
# Phase C — flow-release operator levers: review_tier + Scout injection
# ---------------------------------------------------------------------------


def test_flow_release_sets_review_tier_on_children(client):
    """A chain-wide review_tier on flow-release stamps every child's column."""
    root, child_ids = _setup_gated_root()
    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"review_tier": "critical"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_tier"] == "critical"
    with kb.connect() as conn:
        tiers = {c: kb.get_task(conn, c).review_tier for c in child_ids}
    assert all(t == "critical" for t in tiers.values()), tiers


def test_flow_release_without_options_is_backward_compatible(client):
    """No review_tier / no inject_scout → byte-identical: NO new keys in the
    response or the flow_gate_released event, children untagged, no scout."""
    root, child_ids = _setup_gated_root()
    r = client.post(f"/api/plugins/kanban/tasks/{root}/flow-release")
    assert r.status_code == 200, r.text
    body = r.json()
    # byte-identical: the new keys are absent entirely (not present-as-null)
    assert "review_tier" not in body
    assert "scout_id" not in body
    with kb.connect() as conn:
        assert all(kb.get_task(conn, c).review_tier is None for c in child_ids)
        events = [
            e for e in kb.list_events(conn, root) if e.kind == "flow_gate_released"
        ]
        assert events
        assert "review_tier" not in events[-1].payload
        assert "scout_id" not in events[-1].payload


def test_flow_release_review_tier_only_stamps_children_started_this_call(client):
    """Tier is applied only to children RELEASED this call (chain-start), never
    re-mutating already-started children on a later release (no mid-flight edit)."""
    root, child_ids = _setup_gated_root()
    # First release starts every child at critical.
    r1 = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"review_tier": "critical"},
    )
    assert r1.status_code == 200, r1.text
    # An entry child progresses to running and its tier is cleared.
    with kb.connect() as conn:
        kb.set_task_review_tier(conn, child_ids[0], None)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running' WHERE id=?", (child_ids[0],)
            )
    # A second release with a different tier releases nothing (none scheduled) →
    # must NOT re-stamp the already-started child.
    r2 = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"review_tier": "review"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["released"] == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, child_ids[0]).review_tier is None


def test_flow_release_rejects_invalid_review_tier(client):
    """An unknown tier is rejected by the typed body (no garbage written)."""
    root, _ = _setup_gated_root()
    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"review_tier": "bogus"},
    )
    assert r.status_code == 422, r.text


def test_flow_release_injects_scout_predecessor(client):
    """inject_scout prepends one scout task as predecessor of the entry children
    (those with no in-chain parent), demoting them to todo; scout itself is ready."""
    root, child_ids = _setup_gated_root()
    # child_ids: [0]=a (entry), [1]=b (entry), [2]=c depends on [0,1] (not entry)
    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"inject_scout": True},
    )
    assert r.status_code == 200, r.text
    scout_id = r.json()["scout_id"]
    assert scout_id, r.json()
    with kb.connect() as conn:
        scout = kb.get_task(conn, scout_id)
        assert scout.assignee == "scout"
        assert scout.status == "ready"
        # scout is a predecessor of BOTH entry children, not the dependent one
        assert scout_id in kb.parent_ids(conn, child_ids[0])
        assert scout_id in kb.parent_ids(conn, child_ids[1])
        assert scout_id not in kb.parent_ids(conn, child_ids[2])
        # entry children demoted ready->todo (waiting on the scout); scout has no parents
        assert kb.get_task(conn, child_ids[0]).status == "todo"
        assert kb.get_task(conn, child_ids[1]).status == "todo"
        assert kb.parent_ids(conn, scout_id) == []


def test_flow_release_critical_plus_inject_scout_no_double_scout(client, monkeypatch):
    """Combined lever (Phase-C-followup a): review_tier=critical + inject_scout with
    auto_scout_on_critical ON must NOT give any child a SECOND scout. The per-child
    auto-scout (from the chain-wide tier stamp) fires first; the explicit inject_scout
    then dedups against it. Each code-role child ends with exactly one scout parent;
    non-code roles (reviewer) are skipped by the code_roles guard entirely."""
    monkeypatch.setattr(
        kb,
        "_review_gate_config",
        lambda: {
            "enabled": True,
            "code_roles": frozenset({"coder", "premium"}),
            "verifier_profile": "verifier",
            "auto_tier": False,
            "auto_scout_on_critical": True,
        },
    )
    root, child_ids = _setup_gated_root()
    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"review_tier": "critical", "inject_scout": True},
    )
    assert r.status_code == 200, r.text
    with kb.connect() as conn:
        for cid in child_ids[:2]:
            scouts = [
                p
                for p in kb.parent_ids(conn, cid)
                if kb.get_task(conn, p).assignee == "scout"
            ]
            assert len(scouts) == 1, (cid, scouts)  # exactly one scout, never two
        reviewer_scouts = [
            p
            for p in kb.parent_ids(conn, child_ids[2])
            if kb.get_task(conn, p).assignee == "scout"
        ]
        assert reviewer_scouts == [], reviewer_scouts  # code_roles guard: no scout


def test_flow_release_scout_body_inherits_entry_child_scope(client):
    """The injected scout's body inherits each entry child's id/title/scope plus
    the source-of-truth warning, so a fanned-out scout reconns the real slices
    instead of broadening from its own generic title."""
    root, child_ids = _setup_gated_root()
    # child_ids[0], [1] are entry children (no in-chain parent); [2] depends on them.
    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"inject_scout": True},
    )
    assert r.status_code == 200, r.text
    scout_id = r.json()["scout_id"]
    with kb.connect() as conn:
        body = kb.get_task(conn, scout_id).body or ""
    # both entry children named in the scout body, the non-entry one is not a target
    assert child_ids[0] in body
    assert child_ids[1] in body
    # fan-out framing + the scope-source warning
    assert "VOR 2 Ziel-Tasks" in body
    assert "Source of Truth" in body


# ---------------------------------------------------------------------------
# Phase C — capture-step levers: tier+scout chosen at "Aufgabe erfassen" carry
# through to execution for the gated/parked paths (mirror of the release panel).
# ---------------------------------------------------------------------------


def test_create_task_park_stamps_review_tier(client):
    """A PARKED capture (POST /tasks) carries the chosen review tier onto the
    parked task so the staged-review resolver governs it at later dispatch."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "park me", "park": True, "review_tier": "review"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
    assert t.review_tier == "review"
    assert t.status == "scheduled"  # parked, not auto-dispatched


def test_create_task_without_review_tier_is_unchanged(client):
    """No review_tier on a plain create → the column stays None (byte-identical)."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "plain"})
    assert r.status_code == 200, r.text
    with kb.connect() as conn:
        assert kb.get_task(conn, r.json()["task"]["id"]).review_tier is None


def _patch_planner(monkeypatch, *, gate):
    """Stub kanban_decompose.plan_and_document so flow-capture runs its create/
    park/stamp/intent logic without the real LLM fan-out. The capture-step
    stamping happens BEFORE the planner is called, so an empty outcome is fine."""
    from types import SimpleNamespace

    def _fake(task_id, **kw):
        return SimpleNamespace(
            ok=True,
            task_id=task_id,
            reason=None,
            fanout=False,
            child_ids=[],
            new_title=None,
            spec_relpath=None,
            gated=gate,
        )

    monkeypatch.setattr("hermes_cli.kanban_decompose.plan_and_document", _fake)


def test_flow_suggest_tier_classifies_title_and_description(client):
    """GET flow-suggest-tier runs the deterministic risk heuristic over
    title+description so the capture sheet can pre-fill the tier proposal."""
    r = client.get(
        "/api/plugins/kanban/flow/suggest-tier",
        params={"title": "tweak label", "description": "reword a button"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "standard"
    r2 = client.get(
        "/api/plugins/kanban/flow/suggest-tier",
        params={
            "title": "ship it",
            "description": "run a database migration and deploy",
        },
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["tier"] == "critical"


def test_flow_capture_persists_description_into_body(client, monkeypatch):
    """A capture description is stored as the root body so the risk heuristic has
    substance to classify at gate/resolve time (not just the title)."""
    _patch_planner(monkeypatch, gate=True)
    r = client.post(
        "/api/plugins/kanban/tasks/flow-capture",
        json={
            "title": "Baue X",
            "method": "lean",
            "gate": True,
            "description": "run a database migration and deploy",
        },
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    with kb.connect() as conn:
        assert "database migration" in (kb.get_task(conn, tid).body or "")


def test_flow_capture_stamps_root_tier_and_records_intent(client, monkeypatch):
    """Phase C: the capture sheet's tier+scout ride along to /flow-capture — the
    root is tier-stamped at once (so the chain Review-pill shows) and the intent
    is persisted as a flow_capture_opts event for the gated release to honour."""
    _patch_planner(monkeypatch, gate=True)
    r = client.post(
        "/api/plugins/kanban/tasks/flow-capture",
        json={
            "title": "Baue X",
            "method": "lean",
            "gate": True,
            "review_tier": "critical",
            "inject_scout": True,
        },
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).review_tier == "critical"
        opts = [e for e in kb.list_events(conn, tid) if e.kind == "flow_capture_opts"]
    assert opts, "capture intent must be persisted"
    assert opts[-1].payload["review_tier"] == "critical"
    assert opts[-1].payload["inject_scout"] is True


def test_flow_capture_without_levers_records_no_intent(client, monkeypatch):
    """No tier/scout at capture → no flow_capture_opts event, root untagged
    (byte-identical to the pre-Phase-C flow-capture)."""
    _patch_planner(monkeypatch, gate=True)
    r = client.post(
        "/api/plugins/kanban/tasks/flow-capture",
        json={"title": "Baue Y", "method": "lean", "gate": True},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).review_tier is None
        assert not [
            e for e in kb.list_events(conn, tid) if e.kind == "flow_capture_opts"
        ]


def test_flow_capture_intent_applied_on_bare_release(client):
    """The core carry-through: a gated chain captured with tier+scout (persisted
    as flow_capture_opts) has them applied when the operator just clicks "Kette
    starten" — the release call carries no levers, the capture intent fills it."""
    root, child_ids = _setup_gated_root()
    with kb.connect() as conn, kb.write_txn(conn):
        kb._append_event(
            conn,
            root,
            "flow_capture_opts",
            {"review_tier": "critical", "inject_scout": True},
        )

    r = client.post(f"/api/plugins/kanban/tasks/{root}/flow-release")  # no body levers
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["review_tier"] == "critical"
    assert body["scout_id"]
    with kb.connect() as conn:
        assert all(kb.get_task(conn, c).review_tier == "critical" for c in child_ids)
        # entry children (no in-chain parent) gained the captured scout predecessor
        assert kb.scout_predecessor_id(conn, child_ids[0]) == body["scout_id"]


def test_flow_release_explicit_levers_override_capture_intent(client):
    """An explicit release-time tier still wins over the capture intent — the
    operator can change their mind at "Kette starten"."""
    root, child_ids = _setup_gated_root()
    with kb.connect() as conn, kb.write_txn(conn):
        kb._append_event(
            conn,
            root,
            "flow_capture_opts",
            {"review_tier": "review", "inject_scout": False},
        )

    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"review_tier": "critical"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_tier"] == "critical"
    with kb.connect() as conn:
        assert all(kb.get_task(conn, c).review_tier == "critical" for c in child_ids)


def test_flow_release_explicit_false_vetoes_captured_scout(client):
    """Tri-state inject_scout: an explicit ``inject_scout=False`` at release
    VETOES a captured scout intent — distinct from omitting it (which applies the
    intent). Guards the precedence contract: an explicit release value wins."""
    root, child_ids = _setup_gated_root()
    with kb.connect() as conn, kb.write_txn(conn):
        kb._append_event(
            conn, root, "flow_capture_opts", {"review_tier": None, "inject_scout": True}
        )

    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-release",
        json={"inject_scout": False},
    )
    assert r.status_code == 200, r.text
    assert "scout_id" not in r.json()  # vetoed → no scout spawned
    with kb.connect() as conn:
        assert kb.scout_predecessor_id(conn, child_ids[0]) is None
        assert kb.scout_predecessor_id(conn, child_ids[1]) is None


def test_flow_release_clears_freigabe_operator_hold_at_root(client):
    """A freigabe:operator root released via the flow gate must leave the held
    state exactly like release_freigabe_hold does. Otherwise it stays
    scheduled+freigabe=operator and keeps masquerading as a pending proposal in
    held_operator_proposals — and stays veto/approve-able, which would archive
    or double-release an already-building chain. Regression for the two-release-
    paths invariant gap (freigabe path vs flow-gate path diverged on the root)."""
    from hermes_cli import strategist_surface

    root, child_ids = _setup_gated_root()
    with kb.connect() as conn:
        # Model a real held freigabe:operator proposal: root parked in scheduled.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
                (root,),
            )
        # Pre: it surfaces as a held operator proposal (scheduled + freigabe).
        assert any(
            p["id"] == root for p in strategist_surface.held_operator_proposals(conn)
        )

    r = client.post(f"/api/plugins/kanban/tasks/{root}/flow-release")
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 3

    with kb.connect() as conn:
        # Root flipped scheduled -> todo (mirrors release_freigabe_hold) ...
        assert kb.get_task(conn, root).status == "todo", kb.get_task(conn, root).status
        # ... so it is no longer a held proposal ...
        assert not any(
            p["id"] == root for p in strategist_surface.held_operator_proposals(conn)
        )
        # ... and the release is recorded as a freigabe release.
        kinds = [e.kind for e in kb.list_events(conn, root)]
        assert "freigabe_released" in kinds


def test_flow_release_leaves_non_freigabe_root_scheduled(client):
    """A plain flow-capture root (no freigabe:operator) is NOT promoted by the
    flow gate — release_freigabe_hold is a no-op for it. Only its children are
    released; the root keeps waking on child completion. Guards the fix's blast
    radius so it touches operator-held roots only."""
    root, child_ids = _setup_gated_root()
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (root,))

    r = client.post(f"/api/plugins/kanban/tasks/{root}/flow-release")
    assert r.status_code == 200 and r.json()["released"] == 3

    with kb.connect() as conn:
        # No freigabe:operator hold -> release_freigabe_hold is a no-op: the root
        # stays scheduled and no freigabe release is recorded.
        assert kb.get_task(conn, root).status == "scheduled"
        kinds = [e.kind for e in kb.list_events(conn, root)]
        assert "freigabe_released" not in kinds


def test_flow_gate_sizing_merge_and_split_before_release(client):
    root, child_ids = _setup_gated_root()

    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-gate/sizing",
        json={"action": "merge", "task_ids": child_ids[:2]},
    )
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["action"] == "merge"
    assert merged["kept_id"] == child_ids[0]
    assert merged["archived_id"] == child_ids[1]
    assert [c["id"] for c in merged["gate"]["children"]] == [child_ids[0], child_ids[2]]

    with kb.connect() as conn:
        assert kb.get_task(conn, child_ids[1]).status == "archived"
        assert child_ids[1] not in kb.parent_ids(conn, child_ids[2])
        assert child_ids[0] in kb.parent_ids(conn, child_ids[2])

    r = client.post(
        f"/api/plugins/kanban/tasks/{root}/flow-gate/sizing",
        json={
            "action": "split",
            "task_ids": [child_ids[0]],
            "title": "a follow-up split",
        },
    )
    assert r.status_code == 200, r.text
    split = r.json()
    assert split["action"] == "split"
    new_id = split["new_id"]
    assert new_id

    with kb.connect() as conn:
        new_task = kb.get_task(conn, new_id)
        assert new_task.status == "scheduled"
        assert new_task.title == "a follow-up split"
        assert new_id in kb.parent_ids(conn, root)


def test_flow_gate_timeout_sweep_releases_old_roots(client):
    root, child_ids = _setup_gated_root()
    now = int(time.time())
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - 4000, root))

    r = client.post(
        "/api/plugins/kanban/tasks/flow-gate/timeout-sweep",
        json={"timeout_seconds": 1800},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["released"] == 3
    assert body["released_roots"][0]["task_id"] == root

    with kb.connect() as conn:
        st = {c: kb.get_task(conn, c).status for c in child_ids}
    assert st[child_ids[0]] == "ready"
    assert st[child_ids[1]] == "ready"
    assert st[child_ids[2]] == "todo"


def test_flow_gate_timeout_sweep_scopes_to_flow_planspec_roots(client):
    """B4-F2: the timeout-sweep releases ONLY roots that carry an explicit
    flow/planspec marker (``flow_plan``/``specified``-event or a flow/planspec
    tenant). A foreign root that merely holds ``scheduled`` children for
    unrelated reasons must be left untouched."""
    now = int(time.time())
    old = now - 4000

    # Eligible via tenant.
    flow_root, flow_children = _setup_gated_root(tenant="flow-capture")
    plan_root, plan_children = _setup_gated_root(tenant="planspec")
    # Eligible via explicit event marker despite a foreign tenant.
    marked_root, marked_children = _setup_gated_root(tenant="manual")
    ingested_root, ingested_children = _setup_gated_root(tenant="manual")
    # NOT eligible: foreign tenant, no marker — a parent that just happens to
    # hold scheduled children. Must NOT be released by the sweep.
    foreign_root, foreign_children = _setup_gated_root(tenant="manual")

    roots = [flow_root, plan_root, marked_root, ingested_root, foreign_root]
    with kb.connect() as conn:
        with kb.write_txn(conn):
            for r in roots:
                conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (old, r))
        kb.add_event(conn, marked_root, "flow_plan", {"spec": "x.md", "gated": True})
        kb.add_event(conn, ingested_root, "specified", {"source": "planspec_ingest"})

    r = client.post(
        "/api/plugins/kanban/tasks/flow-gate/timeout-sweep",
        json={"timeout_seconds": 1800},
    )
    assert r.status_code == 200, r.text
    released_roots = {row["task_id"] for row in r.json()["released_roots"]}
    assert flow_root in released_roots
    assert plan_root in released_roots
    assert marked_root in released_roots
    assert ingested_root in released_roots
    assert foreign_root not in released_roots

    with kb.connect() as conn:
        # Foreign children remain held; eligible flow children get released.
        assert all(kb.get_task(conn, c).status == "scheduled" for c in foreign_children)
        assert kb.get_task(conn, flow_children[0]).status == "ready"
        assert kb.get_task(conn, ingested_children[0]).status == "ready"


def test_flow_gate_timeout_sweep_skips_freigabe_operator_roots(client):
    """A strategist PlanSpec lands with ``freigabe='operator'`` so the root is
    parked in ``scheduled`` for explicit operator approve/veto. The autonomous
    timeout-sweep must NOT release such a hold behind the operator's back — only
    the explicit /approve (``release_freigabe_hold``) path may.

    Regression for the 2026-06-22 leak where the timeout-sweep released a
    ``freigabe='operator'`` PlanSpec ~42 min after creation, dispatching the
    held chain without operator sign-off."""
    now = int(time.time())
    old = now - 4000
    plan_root, plan_children = _setup_gated_root(tenant="planspec")
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET created_at=?, freigabe='operator' WHERE id=?",
                (old, plan_root),
            )
        pre_root_status = kb.get_task(conn, plan_root).status

    r = client.post(
        "/api/plugins/kanban/tasks/flow-gate/timeout-sweep",
        json={"timeout_seconds": 1800},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    released_roots = {row["task_id"] for row in body["released_roots"]}
    assert plan_root not in released_roots
    assert body["released"] == 0

    with kb.connect() as conn:
        # Children stay held, root untouched, no freigabe_released event.
        assert all(kb.get_task(conn, c).status == "scheduled" for c in plan_children)
        assert kb.get_task(conn, plan_root).status == pre_root_status
        freigabe = conn.execute(
            "SELECT freigabe FROM tasks WHERE id=?", (plan_root,)
        ).fetchone()["freigabe"]
        assert freigabe == "operator"
        released_ev = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id=? AND kind='freigabe_released' LIMIT 1",
            (plan_root,),
        ).fetchone()
        assert released_ev is None, (
            "operator hold must not be auto-released by the sweep"
        )


def test_strategist_proposal_complete_route_closes_live_fixture_hold(client):
    fixture = _review_efficiency_fixture("complete_freigabe")
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title=fixture["title"],
            body=f"Live fixture root {fixture['root_task_id']}",
            triage=True,
            freigabe=fixture["freigabe"],
            created_by=fixture["created_by"],
        )
        kids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="premium",
            children=[
                {"title": child["title"], "assignee": child["assignee"]}
                for child in fixture["children"]
            ],
            initial_child_status="scheduled",
            expected_root_status="triage",
        )
        assert kids is not None

    r = client.post(
        f"/api/plugins/kanban/strategist/proposals/{root}/complete",
        json={"note": fixture["note"]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "task_id": root, "completed": True}
    proposal_ids = {
        p["id"]
        for p in client.get("/api/plugins/kanban/strategist/proposals").json()[
            "proposals"
        ]
    }
    assert root not in proposal_ids

    with kb.connect() as conn:
        assert kb.get_task(conn, root).status == "archived"
        assert all(kb.get_task(conn, child).status == "archived" for child in kids)
        kinds = [event.kind for event in kb.list_events(conn, root)]
        assert "freigabe_completed" in kinds
        assert any(
            fixture["note"] in comment.body
            for comment in kb.list_comments(conn, root)
        )


def test_chain_graph_returns_dependency_dag_with_runtime_heartbeat(client):
    root, child_ids = _setup_gated_root()
    now = int(time.time())
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child_ids[2],))
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, claim_lock, claim_expires,
                    last_heartbeat_at, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?)
                """,
                (child_ids[0], "coder", "lock-a", now + 300, now - 10, now - 120),
            )

    r = client.get(f"/api/plugins/kanban/tasks/{root}/chain-graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema"] == "kanban-chain-graph-v1"
    assert body["root_id"] == root
    node_ids = {node["id"] for node in body["nodes"]}
    assert node_ids == {root, *child_ids}
    edges = {(edge["from"], edge["to"]) for edge in body["edges"]}
    assert (child_ids[0], child_ids[2]) in edges
    assert (child_ids[1], child_ids[2]) in edges
    assert all((child_id, root) in edges for child_id in child_ids)
    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id[root]["level"] > by_id[child_ids[2]]["level"]
    assert by_id[child_ids[0]]["progress"] == {"done": 1, "total": 2}
    assert by_id[child_ids[1]]["progress"] == {"done": 1, "total": 2}
    assert by_id[child_ids[2]]["progress"] == {"done": 0, "total": 1}
    assert by_id[root]["progress"] is None
    latest = by_id[child_ids[0]]["latest_run"]
    assert latest["profile"] == "coder"
    assert latest["runtime_seconds"] >= 100
    assert latest["heartbeat_age_seconds"] >= 0


def _insert_cost_run(conn, task_id, *, profile, input_tokens, output_tokens, cost_usd):
    """Insert a run row with cost data; does NOT commit (caller manages txn)."""
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, outcome, started_at, ended_at, "
        "input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 'done', 'completed', 1000, 2000, ?, ?, ?)",
        (task_id, profile, input_tokens, output_tokens, cost_usd),
    )


def test_chain_costs_endpoint_returns_breakdown(client, kanban_home):
    """GET /tasks/{id}/chain-costs returns schema kanban-chain-costs-v1 with
    correct totals and by_lane breakdown aggregated from task_runs."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="costed-chain", assignee="default", triage=True
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="default",
            children=[
                {"title": "worker A", "assignee": "coder", "parents": []},
                {"title": "worker B", "assignee": "coder", "parents": []},
            ],
            author="decomposer",
        )
        a, b = child_ids
        with kb.write_txn(conn):
            _insert_cost_run(
                conn,
                a,
                profile="coder",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.01,
            )
            _insert_cost_run(
                conn,
                b,
                profile="coder",
                input_tokens=600,
                output_tokens=100,
                cost_usd=0.006,
            )
            _insert_cost_run(
                conn,
                b,
                profile="verifier",
                input_tokens=200,
                output_tokens=30,
                cost_usd=0.002,
            )

    r = client.get(f"/api/plugins/kanban/tasks/{root}/chain-costs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema"] == "kanban-chain-costs-v1"
    assert body["root_id"] == root

    totals = body["totals"]
    assert totals["run_count"] == 3
    assert totals["input_tokens"] == 1800
    assert totals["output_tokens"] == 330
    assert abs(totals["cost_usd"] - 0.018) < 1e-9

    by_lane = body["by_lane"]
    profiles = [l["profile"] for l in by_lane]
    assert "coder" in profiles
    assert "verifier" in profiles
    coder = next(l for l in by_lane if l["profile"] == "coder")
    assert coder["run_count"] == 2
    # most expensive lane first
    assert by_lane[0]["profile"] == "coder"


def test_chain_costs_endpoint_404_for_unknown_task(client):
    r = client.get("/api/plugins/kanban/tasks/t_no_such_task/chain-costs")
    assert r.status_code == 404


def test_chain_costs_endpoint_resolves_non_root_to_root(client, kanban_home):
    """Endpoint resolves a non-root member task to its chain root."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="chain-root-resolve", assignee="default", triage=True
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="default",
            children=[{"title": "leaf", "assignee": "coder", "parents": []}],
            author="decomposer",
        )
        (leaf,) = child_ids
        with kb.write_txn(conn):
            _insert_cost_run(
                conn,
                leaf,
                profile="coder",
                input_tokens=500,
                output_tokens=80,
                cost_usd=0.005,
            )

    # Querying via the leaf should resolve to the root chain
    r = client.get(f"/api/plugins/kanban/tasks/{leaf}/chain-costs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root_id"] == root
    assert body["totals"]["run_count"] == 1


def test_chain_graph_nodes_include_cost_fields(client, kanban_home):
    """chain-graph nodes now carry cost_usd, input_tokens, output_tokens
    per-task aggregated from task_runs — existing schema contract unchanged."""
    root, child_ids = _setup_gated_root()
    with kb.connect() as conn:
        with kb.write_txn(conn):
            _insert_cost_run(
                conn,
                child_ids[0],
                profile="coder",
                input_tokens=800,
                output_tokens=150,
                cost_usd=0.008,
            )

    r = client.get(f"/api/plugins/kanban/tasks/{root}/chain-graph")
    assert r.status_code == 200, r.text
    body = r.json()
    # Schema unchanged
    assert body["schema"] == "kanban-chain-graph-v1"
    by_id = {n["id"]: n for n in body["nodes"]}

    # Node with a run carries cost fields
    n0 = by_id[child_ids[0]]
    assert n0["cost_usd"] == pytest.approx(0.008)
    assert n0["input_tokens"] == 800
    assert n0["output_tokens"] == 150

    # Nodes without runs have cost fields present and zeroed (not missing)
    n1 = by_id[child_ids[1]]
    assert "cost_usd" in n1
    assert "input_tokens" in n1
    assert "output_tokens" in n1
    assert n1["cost_usd"] == 0.0
    assert n1["input_tokens"] == 0
    assert n1["output_tokens"] == 0


def _insert_cost_run_with_meta(
    conn, task_id, *, profile, input_tokens, output_tokens, cost_usd, metadata=None
):
    """Insert a run row with cost data and optional metadata; does NOT commit."""
    import json as _json

    meta_str = _json.dumps(metadata) if metadata is not None else None
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, outcome, started_at, ended_at, "
        "input_tokens, output_tokens, cost_usd, metadata) "
        "VALUES (?, ?, 'done', 'completed', 1000, 2000, ?, ?, ?, ?)",
        (task_id, profile, input_tokens, output_tokens, cost_usd, meta_str),
    )


def test_chain_graph_nodes_include_cost_equivalent_fields(client, kanban_home):
    """chain-graph nodes carry cost_usd_equivalent and cost_effective_usd for
    subscription runs (cost_usd=0, metadata.cost_usd_equivalent set)."""
    root, child_ids = _setup_gated_root()
    with kb.connect() as conn:
        with kb.write_txn(conn):
            # subscription run on child_ids[0]: cost_usd=0, equivalent=0.55
            _insert_cost_run_with_meta(
                conn,
                child_ids[0],
                profile="claude-cli",
                input_tokens=1200,
                output_tokens=300,
                cost_usd=0.0,
                metadata={"cost_usd_equivalent": 0.55},
            )

    r = client.get(f"/api/plugins/kanban/tasks/{root}/chain-graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema"] == "kanban-chain-graph-v1"
    by_id = {n["id"]: n for n in body["nodes"]}

    n0 = by_id[child_ids[0]]
    assert n0["cost_usd"] == pytest.approx(0.0)
    assert n0["cost_usd_equivalent"] == pytest.approx(0.55)
    assert n0["cost_effective_usd"] == pytest.approx(0.55)

    # Nodes without runs: new fields present and zeroed
    n1 = by_id[child_ids[1]]
    assert "cost_usd_equivalent" in n1
    assert "cost_effective_usd" in n1
    assert n1["cost_usd_equivalent"] == 0.0
    assert n1["cost_effective_usd"] == 0.0


def test_chain_costs_endpoint_includes_equivalent_fields(client, kanban_home):
    """GET /tasks/{id}/chain-costs now returns cost_usd_equivalent and
    cost_effective_usd in totals and by_lane for subscription runs."""
    import json as _json

    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="equiv-chain", assignee="default", triage=True
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="default",
            children=[
                {"title": "sub-worker", "assignee": "claude-cli", "parents": []},
            ],
            author="decomposer",
        )
        (task_a,) = child_ids
        with kb.write_txn(conn):
            _insert_cost_run_with_meta(
                conn,
                task_a,
                profile="claude-cli",
                input_tokens=900,
                output_tokens=180,
                cost_usd=0.0,
                metadata={"cost_usd_equivalent": 0.77},
            )

    r = client.get(f"/api/plugins/kanban/tasks/{root}/chain-costs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema"] == "kanban-chain-costs-v1"

    totals = body["totals"]
    assert totals["cost_usd"] == pytest.approx(0.0)
    assert totals["cost_usd_equivalent"] == pytest.approx(0.77)
    assert totals["cost_effective_usd"] == pytest.approx(0.77)

    by_lane = body["by_lane"]
    lane = next(l for l in by_lane if l["profile"] == "claude-cli")
    assert lane["cost_usd"] == pytest.approx(0.0)
    assert lane["cost_usd_equivalent"] == pytest.approx(0.77)
    assert lane["cost_effective_usd"] == pytest.approx(0.77)


def test_flow_release_unknown_task_404(client):
    r = client.post("/api/plugins/kanban/tasks/t_nope/flow-release")
    assert r.status_code == 404


def test_flow_plan_serves_spec_and_404s_when_absent(client, tmp_path, monkeypatch):
    spec_dir = tmp_path / "flow-plans"
    spec_dir.mkdir()
    monkeypatch.setenv("HERMES_FLOW_PLANS_DIR", str(spec_dir))

    # No spec yet -> 404.
    r = client.get("/api/plugins/kanban/tasks/t_abc123/flow-plan")
    assert r.status_code == 404

    # Write a spec and serve it.
    (spec_dir / "t_abc123.md").write_text(
        "# Flow-Plan\n\n## Narrativ\n\nhi\n", encoding="utf-8"
    )
    r = client.get("/api/plugins/kanban/tasks/t_abc123/flow-plan")
    assert r.status_code == 200, r.text
    assert "## Narrativ" in r.text
    assert "markdown" in r.headers.get("content-type", "")

    # Path-traversal attempt rejected by the id charset guard.
    r = client.get("/api/plugins/kanban/tasks/..%2f..%2fetc/flow-plan")
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# N-E1: GET /decision-queue
# ---------------------------------------------------------------------------


def test_decision_queue_empty(client):
    r = client.get("/api/plugins/kanban/decision-queue")
    assert r.status_code == 200
    data = r.json()
    assert data["decisions"] == []
    assert data["count"] == 0
    assert "checked_at" in data


def test_decision_queue_surfaces_sticky_blocked(client):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="stuck", assignee="coder")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="needs human eyes")
    r = client.get("/api/plugins/kanban/decision-queue")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["decisions"][0]
    assert row["kind"] == "sticky_blocked"
    assert row["task_id"] == t
    assert row["suggested_command"] == f"hermes kanban unblock {t}"


def test_decision_queue_surfaces_failure_escalation_with_human_payload(client):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="runtime stop needs Piet", assignee="coder")
        assert kb.claim_task(conn, t) is not None
        assert not kb._record_task_failure(
            conn,
            t,
            "first spawn failure",
            outcome="spawn_failed",
            failure_limit=2,
            release_claim=True,
            end_run=True,
        )
        assert kb.claim_task(conn, t) is not None
        assert kb._record_task_failure(
            conn,
            t,
            "second spawn failure",
            outcome="spawn_failed",
            failure_limit=2,
            release_claim=True,
            end_run=True,
            event_payload_extra={"run_log": "smoke-run-2"},
        )

    r = client.get("/api/plugins/kanban/decision-queue")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    row = data["decisions"][0]
    assert row["kind"] == "operator_escalation"
    assert row["task_id"] == t
    assert row["reason"].startswith("retry ladder exhausted")
    decision = row["operator_escalation"]
    assert set(decision) == {
        "task",
        "why_now",
        "attempts_already_made",
        "evidence",
        "recommended_human_action",
        "blocked_action_boundary",
    }
    assert decision["task"]["id"] == t
    assert decision["attempts_already_made"] == 2
    assert decision["evidence"]["context"] == {"run_log": "smoke-run-2"}
    assert "inspect the task" in decision["recommended_human_action"]
    assert decision["blocked_action_boundary"] == list(kb.OPERATOR_ONLY_ACTIONS)

    repeat = client.get("/api/plugins/kanban/decision-queue").json()
    assert repeat["count"] == 1
    assert [d["task_id"] for d in repeat["decisions"]] == [t]


def test_decision_queue_enriches_operator_escalation_with_latest_block_reason(client):
    def _seed_operator_escalation(conn, title, *, block_reason_marker):
        task_id = kb.create_task(conn, title=title, assignee="coder")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
        with kb.write_txn(conn):
            if block_reason_marker is not None:
                kb._append_event(
                    conn,
                    task_id,
                    "blocked",
                    {
                        "reason": "stale block reason",
                        "kind": "retryable",
                    },
                )
                kb._append_event(
                    conn,
                    task_id,
                    "blocked",
                    {
                        # Live task_events payload shape for retryable parked blocks.
                        "reason": block_reason_marker,
                        "kind": "retryable",
                    },
                )
            kb._append_event(
                conn,
                task_id,
                "operator_escalation",
                {
                    "task": {
                        "id": task_id,
                        "title": title,
                        "status": "blocked",
                        "assignee": "coder",
                    },
                    "why_now": "settled block without actionable last_error",
                    "attempts_already_made": 1,
                    "evidence": {
                        "trigger_outcome": "blocked",
                        "last_error": "",
                        "blocked_kind": "retryable",
                    },
                    "recommended_human_action": "inspect the task",
                    "blocked_action_boundary": list(kb.OPERATOR_ONLY_ACTIONS),
                },
            )
        return task_id

    with kb.connect() as conn:
        with_reason = _seed_operator_escalation(
            conn,
            "release gate retryable block",
            block_reason_marker="awaiting release-gate GO",
        )
        without_blocked_event = _seed_operator_escalation(
            conn,
            "budget runaway park",
            block_reason_marker=None,
        )
        empty_reason = _seed_operator_escalation(
            conn,
            "empty block reason",
            block_reason_marker="  ",
        )

    r = client.get("/api/plugins/kanban/decision-queue")
    assert r.status_code == 200, r.text
    by_task = {row["task_id"]: row for row in r.json()["decisions"]}

    assert (
        by_task[with_reason]["operator_escalation"]["evidence"]["block_reason"]
        == "awaiting release-gate GO"
    )
    assert (
        by_task[without_blocked_event]["operator_escalation"]["evidence"]["block_reason"]
        is None
    )
    assert (
        by_task[empty_reason]["operator_escalation"]["evidence"]["block_reason"]
        is None
    )


# ---------------------------------------------------------------------------
# N-E3: GET /epics + /epics/{id}
# ---------------------------------------------------------------------------


def test_epics_list_empty(client):
    r = client.get("/api/plugins/kanban/epics")
    assert r.status_code == 200
    assert r.json() == {"epics": [], "count": 0}


def test_epics_list_and_show_with_rollup(client):
    with kb.connect() as conn:
        eid = kb.create_epic(conn, title="Reliability")
        t = kb.create_task(conn, title="member", assignee="coder", epic_id=eid)
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, result="done", summary="done")
    r = client.get("/api/plugins/kanban/epics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["epics"][0]["id"] == eid
    assert data["epics"][0]["done_tasks"] == 1

    r2 = client.get(f"/api/plugins/kanban/epics/{eid}")
    assert r2.status_code == 200
    epic = r2.json()["epic"]
    assert epic["task_count"] == 1
    assert [x["id"] for x in epic["tasks"]] == [t]

    assert client.get("/api/plugins/kanban/epics/e_missing").status_code == 404


def test_epic_create_endpoint(client):
    r = client.post("/api/plugins/kanban/epics", json={"title": "Board epic"})
    assert r.status_code == 200
    epic = r.json()["epic"]
    assert epic["id"].startswith("e_")
    assert epic["title"] == "Board epic"
    assert epic["status"] == "open"
    assert epic["task_count"] == 0
    # Body is optional but passed through.
    r2 = client.post(
        "/api/plugins/kanban/epics",
        json={"title": "With body", "body": "longer intent"},
    )
    assert r2.json()["epic"]["body"] == "longer intent"
    # Empty title → 400, not a 500.
    assert (
        client.post("/api/plugins/kanban/epics", json={"title": "  "}).status_code
        == 400
    )


def test_epic_close_endpoint(client):
    eid = client.post(
        "/api/plugins/kanban/epics",
        json={"title": "to close"},
    ).json()["epic"]["id"]
    r = client.post(f"/api/plugins/kanban/epics/{eid}/close")
    assert r.status_code == 200
    assert r.json()["epic"]["status"] == "closed"
    assert client.post("/api/plugins/kanban/epics/e_missing/close").status_code == 404


def test_patch_task_epic_id_assign_and_detach(client):
    eid = client.post(
        "/api/plugins/kanban/epics",
        json={"title": "target"},
    ).json()["epic"]["id"]
    with kb.connect() as conn:
        t = kb.create_task(conn, title="chain root", assignee="coder")

    # Attach.
    r = client.patch(f"/api/plugins/kanban/tasks/{t}", json={"epic_id": eid})
    assert r.status_code == 200
    assert r.json()["task"]["epic_id"] == eid
    # Rollup sees the member.
    assert (
        client.get(
            f"/api/plugins/kanban/epics/{eid}",
        ).json()["epic"]["task_count"]
        == 1
    )

    # Absent epic_id leaves membership untouched (no accidental detach).
    r2 = client.patch(f"/api/plugins/kanban/tasks/{t}", json={"priority": 1})
    assert r2.json()["task"]["epic_id"] == eid

    # Explicit null detaches.
    r3 = client.patch(f"/api/plugins/kanban/tasks/{t}", json={"epic_id": None})
    assert r3.status_code == 200
    assert r3.json()["task"]["epic_id"] is None


def test_patch_task_epic_id_validates_target(client):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="task", assignee="coder")
    # Unknown epic → 409 with the db-layer message.
    r = client.patch(f"/api/plugins/kanban/tasks/{t}", json={"epic_id": "e_ghost"})
    assert r.status_code == 409
    assert "not found" in r.json()["detail"]
    # Closed epic → 409.
    eid = client.post(
        "/api/plugins/kanban/epics",
        json={"title": "closed"},
    ).json()["epic"]["id"]
    client.post(f"/api/plugins/kanban/epics/{eid}/close")
    r2 = client.patch(f"/api/plugins/kanban/tasks/{t}", json={"epic_id": eid})
    assert r2.status_code == 409
    assert "closed" in r2.json()["detail"]


def test_workers_active_carries_note_and_eta(client):
    """Phase A: /workers/active liefert die jüngste Heartbeat-Note + p50/p90
    des Profils; ohne Historie bleiben die ETA-Felder ehrlich null."""
    now = int(time.time())
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="progressing")
        with kb.write_txn(conn):
            # ETA-Historie: 3 abgeschlossene coder-Runs à 120/240/600s
            for dur in (120, 240, 600):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, "
                    "started_at, ended_at) VALUES (?, 'coder', 'done', 'completed', ?, ?)",
                    (t, now - 9000, now - 9000 + dur),
                )
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid) "
                "VALUES (?, 'coder', 'running', ?, 4242)",
                (t, now - 60),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?",
                (run_id, t),
            )
        assert kb.heartbeat_worker(
            conn, t, note="Edit: WorkerCard.tsx", expected_run_id=run_id
        )
        assert kb.heartbeat_worker(
            conn, t, note="Bash: vitest run", expected_run_id=run_id
        )
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/workers/active").json()
    worker = next(w for w in data["workers"] if w["run_id"] == run_id)
    assert worker["last_heartbeat_note"] == "Bash: vitest run"  # jüngste Note
    assert worker["last_heartbeat_note_at"] is not None
    assert worker["eta_p50_seconds"] == 240
    assert worker["eta_p90_seconds"] == 600


def test_workers_active_run_progress_from_runtime_cap(client):
    """S2 AC-2: run_progress ist ehrlich 0..1, abgeleitet aus started_at +
    max_runtime_seconds (vorhandene persistierte Spalten). Bei vorhandenem
    Cap liefert der Endpunkt die korrekte Ratio; ohne Cap bleibt es null."""
    now = int(time.time())
    conn = kb.connect()
    try:
        t_capped = kb.create_task(conn, title="capped run")
        t_uncapped = kb.create_task(conn, title="uncapped run")
        with kb.write_txn(conn):
            # Capped: started vor 30s, max_runtime 300s → 0.10
            rid_capped = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, "
                "worker_pid, max_runtime_seconds) "
                "VALUES (?, 'coder', 'running', ?, 9001, 300)",
                (t_capped, now - 30),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
                (rid_capped, t_capped),
            )
            # Uncapped: kein max_runtime_seconds → null (claude-cli / scorer)
            rid_uncapped = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, "
                "worker_pid) VALUES (?, 'scout', 'running', ?, 9002)",
                (t_uncapped, now - 200),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
                (rid_uncapped, t_uncapped),
            )
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/workers/active").json()
    capped = next(w for w in data["workers"] if w["run_id"] == rid_capped)
    uncapped = next(w for w in data["workers"] if w["run_id"] == rid_uncapped)

    # 30/300 = 0.10 (mit Toleranz für die Sekunde zwischen Insert und Request)
    assert capped["run_progress"] is not None
    assert abs(capped["run_progress"] - 0.10) < 0.02

    # Kein Cap → null, KEIN geratener Wert
    assert uncapped["run_progress"] is None


def test_workers_active_carries_input_output_tokens_and_heartbeat_ticks(client):
    """/workers/active selects input_tokens/output_tokens from task_runs and
    returns heartbeat_ticks capped to the newest 20 timestamps per run."""
    now = int(time.time())
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="tokened worker")
        quiet_t = kb.create_task(conn, title="quiet worker")
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, "
                "worker_pid, input_tokens, output_tokens) "
                "VALUES (?, 'coder', 'running', ?, 7171, 1234, 567)",
                (t, now - 60),
            ).lastrowid
            quiet_run_id = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, "
                "worker_pid, input_tokens, output_tokens) "
                "VALUES (?, 'coder', 'running', ?, 7172, 10, 20)",
                (quiet_t, now - 60),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
                (run_id, t),
            )
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
                (quiet_run_id, quiet_t),
            )
            # Quiet worker beats first; a global newest-N cap would drop these
            # once the noisy worker appends enough newer beats.
            for i in range(3):
                kb._append_event(
                    conn, quiet_t, "heartbeat", {"note": f"quiet {i}"}, run_id=quiet_run_id
                )
            for i in range(45):
                kb._append_event(conn, t, "heartbeat", {"note": f"step {i}"}, run_id=run_id)
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/workers/active").json()
    worker = next(w for w in data["workers"] if w["run_id"] == run_id)
    quiet = next(w for w in data["workers"] if w["run_id"] == quiet_run_id)
    assert worker["input_tokens"] == 1234
    assert worker["output_tokens"] == 567
    assert len(worker["heartbeat_ticks"]) == 20
    assert worker["heartbeat_ticks"] == sorted(worker["heartbeat_ticks"])
    assert len(quiet["heartbeat_ticks"]) == 3
    assert quiet["heartbeat_ticks"] == sorted(quiet["heartbeat_ticks"])
    assert all(now - 300 <= ts <= now for ts in worker["heartbeat_ticks"])
    assert all(now - 300 <= ts <= now for ts in quiet["heartbeat_ticks"])


def test_live_events_returns_newest_first_respects_since_id_and_allowlist(client):
    """GET /runs/live-events filters kinds to the display allowlist, returns
    events newest-first, and supports incremental since_id polling."""
    now = int(time.time())
    conn = kb.connect()
    try:
        ta = kb.create_task(conn, title="alpha")
        tb = kb.create_task(conn, title="beta")
        with kb.write_txn(conn):
            ra = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid) "
                "VALUES (?, 'coder', 'running', ?, 8001)",
                (ta, now - 300),
            ).lastrowid
            rb = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid) "
                "VALUES (?, 'scout', 'running', ?, 8002)",
                (tb, now - 300),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
                (ra, ta),
            )
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
                (rb, tb),
            )
            # allowed kind
            kb._append_event(conn, ta, "heartbeat", {"note": "alpha beat"}, run_id=ra)
            # allowed kind with a malformed payload as persisted in task_events
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'blocked', '{malformed', ?)",
                (ta, ra, now),
            )
            # allowed kind with null note
            kb._append_event(conn, tb, "claimed", None, run_id=rb)
            # noise kind (excluded by allowlist)
            kb._append_event(conn, tb, "created", {"note": "created noise"}, run_id=rb)
    finally:
        conn.close()

    response = client.get("/api/plugins/kanban/runs/live-events")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    assert [e["kind"] for e in data["events"]] == ["claimed", "blocked", "heartbeat"]
    assert data["events"][2]["task_title"] == "alpha"
    assert data["events"][2]["profile"] == "coder"
    assert data["events"][2]["note"] == "alpha beat"
    assert data["events"][1]["note"] is None
    assert data["events"][0]["task_id"] == tb
    assert "latest_id" in data and data["latest_id"] is not None
    assert "checked_at" in data

    # The board query param selects the board database; tasks are already
    # scoped by connection and do not carry a separate t.board column.
    board_data = client.get(
        f"/api/plugins/kanban/runs/live-events?board={kb.DEFAULT_BOARD}"
    ).json()
    assert board_data["count"] == 3
    assert [e["kind"] for e in board_data["events"]] == ["claimed", "blocked", "heartbeat"]

    # since_id should return only newer events
    latest_id = data["latest_id"]
    since = client.get(f"/api/plugins/kanban/runs/live-events?since_id={latest_id}").json()
    assert since["count"] == 0
    assert since["events"] == []

    board_since = client.get(
        f"/api/plugins/kanban/runs/live-events?board={kb.DEFAULT_BOARD}&since_id={latest_id}"
    ).json()
    assert board_since["count"] == 0
    assert board_since["events"] == []

    # limit should cap results
    limited = client.get("/api/plugins/kanban/runs/live-events?limit=1").json()
    assert limited["count"] == 1
    assert limited["events"][0]["kind"] == "claimed"


def test_chain_graph_latest_run_carries_run_progress(client):
    """S2: chain-graph latest_run liefert ebenfalls run_progress (additiv).
    null bei fehlendem Cap, korrekte Ratio bei vorhandenem Cap."""
    root, child_ids = _setup_gated_root()
    now = int(time.time())
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child_ids[2],))
            # Capped running run auf child[0]: 45s von 300s → 0.15
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
                "claim_expires, last_heartbeat_at, started_at, max_runtime_seconds) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?, 300)",
                (child_ids[0], "coder", "lock-a", now + 300, now - 10, now - 45),
            )
            # Uncapped running run auf child[1] → null
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
                "claim_expires, last_heartbeat_at, started_at) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?)",
                (child_ids[1], "scout", "lock-b", now + 300, now - 10, now - 60),
            )
    r = client.get(f"/api/plugins/kanban/tasks/{root}/chain-graph")
    assert r.status_code == 200, r.text
    by_id = {n["id"]: n for n in r.json()["nodes"]}
    capped_lr = by_id[child_ids[0]]["latest_run"]
    uncapped_lr = by_id[child_ids[1]]["latest_run"]
    assert capped_lr is not None and abs(capped_lr["run_progress"] - 0.15) < 0.02
    assert uncapped_lr is not None and uncapped_lr["run_progress"] is None


def test_workers_active_surfaces_claude_cli_dispatcher_heartbeat(client, monkeypatch):
    """Criterion 3: the dispatcher-side claude-CLI heartbeat surfaces through the
    EXISTING last_heartbeat_* fields — no new endpoint/field/UI concept needed."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder-claude")
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

    now = int(time.time())
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="claude worker live")
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, "
                "claim_lock, worker_pid) VALUES (?, 'coder-claude', 'running', ?, ?, 5151)",
                (t, now - 120, kb._claimer_id()),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET status = 'running', current_run_id = ?, "
                "claim_lock = ?, worker_pid = 5151 WHERE id = ?",
                (run_id, kb._claimer_id(), t),
            )
        # Dispatcher refreshes liveness for the detached claude-CLI worker.
        assert kb.heartbeat_live_claude_cli_workers(conn) == [t]
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/workers/active").json()
    worker = next(w for w in data["workers"] if w["run_id"] == run_id)
    assert worker["last_heartbeat_at"] is not None  # no longer "—" on the card
    assert worker["last_heartbeat_note"].startswith("claude-cli running")
    assert worker["last_heartbeat_note_at"] is not None


def test_task_model_override_roundtrip(client):
    """Phase B: POST /tasks akzeptiert model_override, PATCH setzt/löscht ihn,
    und der Wert steht in der Task-Antwort (Spawn-Resolution liest tasks.model_override)."""
    created = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "needs strong model", "model_override": "claude-fable-5"},
    ).json()["task"]
    assert created["model_override"] == "claude-fable-5"
    tid = created["id"]

    # Eskalation per PATCH umschwenken
    patched = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"model_override": "claude-opus-4-8"},
    )
    assert patched.status_code == 200
    got = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
    assert got["model_override"] == "claude-opus-4-8"

    # Explizites null löscht; Event-Trail dokumentiert beide Schritte
    cleared = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"model_override": None},
    )
    assert cleared.status_code == 200
    got = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
    assert got["model_override"] is None
    conn = kb.connect()
    try:
        kinds = [
            r["payload"]
            for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'model_override_set' ORDER BY id",
                (tid,),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert len(kinds) == 2


# ---------------------------------------------------------------------------
# Demand-Funnel Freigabe-Queue: GET /funnel/drafts + POST .../approve
# ---------------------------------------------------------------------------


def _make_funnel_draft(conn, *, created_by="family", title="wunsch"):
    from hermes_cli import funnel

    tid = funnel.create_wish(conn, title=title, body="b", created_by=created_by)
    kb.add_comment(conn, tid, "coder-claude", "# Draft\n" + "d" * 200)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), tid),
        )
    return tid


def test_funnel_drafts_lists_done_funnel_roots(client):
    conn = kb.connect()
    try:
        tid = _make_funnel_draft(conn)
    finally:
        conn.close()

    data = client.get("/api/plugins/kanban/funnel/drafts").json()
    assert [d["id"] for d in data["drafts"]] == [tid]
    assert data["drafts"][0]["created_by"] == "family"
    assert data["drafts"][0]["draft_excerpt"].startswith("# Draft")


def test_funnel_draft_surface_is_separate_from_failure_decision_queue(client):
    conn = kb.connect()
    try:
        tid = _make_funnel_draft(
            conn,
            created_by="fo-gap-audit",
            title="protected funnel wait",
        )
    finally:
        conn.close()

    drafts = client.get("/api/plugins/kanban/funnel/drafts").json()["drafts"]
    decisions = client.get("/api/plugins/kanban/decision-queue").json()["decisions"]

    assert [d["id"] for d in drafts] == [tid]
    assert drafts[0]["created_by"] == "fo-gap-audit"
    assert drafts[0]["draft_excerpt"].startswith("# Draft")
    assert all(d["task_id"] != tid for d in decisions)


def test_funnel_draft_approve_creates_build_child_then_409_on_repeat(client):
    conn = kb.connect()
    try:
        tid = _make_funnel_draft(conn, created_by="discord-idee")
    finally:
        conn.close()

    r = client.post(f"/api/plugins/kanban/funnel/drafts/{tid}/approve")
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["status"] == "ready"
    assert task["created_by"] == "discord-idee"
    assert task["title"].startswith("Umsetzen:")

    # Liste ist danach leer (Root hat ein Build-Kind) …
    assert client.get("/api/plugins/kanban/funnel/drafts").json()["drafts"] == []
    # … und Doppel-Freigabe wird abgelehnt.
    r2 = client.post(f"/api/plugins/kanban/funnel/drafts/{tid}/approve")
    assert r2.status_code == 409
    assert "bereits freigegeben" in r2.json()["detail"]


def test_funnel_draft_approve_404ish_on_unknown_task(client):
    r = client.post("/api/plugins/kanban/funnel/drafts/t_gibtsnicht/approve")
    assert r.status_code == 409
    assert "nicht gefunden" in r.json()["detail"]


def test_funnel_draft_dismiss_archives_root(client):
    conn = kb.connect()
    try:
        tid = _make_funnel_draft(conn)
    finally:
        conn.close()

    r = client.post(f"/api/plugins/kanban/funnel/drafts/{tid}/dismiss")
    assert r.status_code == 200, r.text
    assert client.get("/api/plugins/kanban/funnel/drafts").json()["drafts"] == []
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "archived"
    finally:
        conn.close()
    # Nochmal verwerfen -> 409 (nicht mehr in der Queue).
    assert (
        client.post(f"/api/plugins/kanban/funnel/drafts/{tid}/dismiss").status_code
        == 409
    )


def test_funnel_draft_patch_saves_operator_edit(client):
    conn = kb.connect()
    try:
        tid = _make_funnel_draft(conn)
    finally:
        conn.close()

    r = client.patch(
        f"/api/plugins/kanban/funnel/drafts/{tid}",
        json={
            "draft_text": "# Operator-Fassung\n" + "o" * 160,
            "operator_note": "ACs explizit aufnehmen.",
        },
    )
    assert r.status_code == 200, r.text
    draft = r.json()["draft"]
    assert draft["id"] == tid
    assert draft["operator_edited"] is True
    assert "# Operator-edited PlanSpec" in draft["draft_text"]
    assert "ACs explizit aufnehmen" in draft["draft_text"]

    listed = client.get("/api/plugins/kanban/funnel/drafts").json()["drafts"]
    assert [d["id"] for d in listed] == [tid]
    assert listed[0]["operator_edited"] is True


def test_funnel_draft_revise_creates_new_root_and_archives_old(client):
    conn = kb.connect()
    try:
        tid = _make_funnel_draft(conn, title="spec prüfen")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/funnel/drafts/{tid}/revise",
        json={
            "draft_text": "# Zwischenstand\n" + "z" * 160,
            "operator_note": "Bitte nochmal Familiennutzen schärfen.",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    task = data["task"]
    assert data["superseded"] == tid
    assert task["status"] == "ready"
    assert task["title"].startswith("Überarbeiten:")
    assert client.get("/api/plugins/kanban/funnel/drafts").json()["drafts"] == []

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "archived"
        assert (
            conn.execute(
                "SELECT 1 FROM task_links WHERE child_id = ?", (task["id"],)
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_funnel_draft_patch_and_revise_409_on_unknown(client):
    r = client.patch(
        "/api/plugins/kanban/funnel/drafts/t_gibtsnicht",
        json={"draft_text": "# Nope\n" + "n" * 160},
    )
    assert r.status_code == 409
    assert "nicht gefunden" in r.json()["detail"]

    r2 = client.post(
        "/api/plugins/kanban/funnel/drafts/t_gibtsnicht/revise",
        json={"draft_text": "# Nope\n" + "n" * 160},
    )
    assert r2.status_code == 409
    assert "nicht gefunden" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# /lanes — profile catalog fast path
# ---------------------------------------------------------------------------


def _plugin_module():
    """The dynamically loaded plugin module behind the `client` fixture."""
    return sys.modules["hermes_dashboard_plugin_kanban_test"]


def _write_lane_profiles(home: Path) -> None:
    coder = home / "profiles" / "coder"
    coder.mkdir(parents=True, exist_ok=True)
    (coder / "config.yaml").write_text(
        "worker_runtime: claude-cli\nclaude_model: claude-fable-5\n"
    )
    (coder / "profile.yaml").write_text("description: builds things\n")
    research = home / "profiles" / "research"
    research.mkdir(exist_ok=True)
    (research / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: gpt-5.4\n"
        "fallback_providers:\n"
        "  - provider: openai-codex\n"
        "    model: gpt-5.5\n"
    )


def _stub_lane_inventory(monkeypatch):
    import hermes_cli.inventory as inventory

    monkeypatch.setattr(inventory, "load_picker_context", lambda: object())
    monkeypatch.setattr(
        inventory,
        "build_models_payload",
        lambda *args, **kwargs: {
            "providers": [
                {
                    "slug": "openrouter",
                    "name": "OpenRouter",
                    "authenticated": True,
                    "models": ["qwen/qwen3.7-max", "moonshotai/kimi-k2.7"],
                },
                {
                    "slug": "openai-codex",
                    "name": "OpenAI Codex",
                    "authenticated": True,
                    "models": ["gpt-5.5"],
                },
            ],
            "model": "gpt-5.4",
            "provider": "openrouter",
        },
    )


def test_lanes_profile_catalog_avoids_list_profiles(client, monkeypatch):
    """GET /lanes must not call list_profiles(): that helper rglobs every
    profile's skills/ tree (~5s with 11 real profiles) and made the lanes
    tab time out. The lean scan reads only the two small YAMLs per profile."""
    _write_lane_profiles(Path(os.environ["HERMES_HOME"]))
    mod = _plugin_module()
    mod._lane_profile_cache = None
    _stub_lane_inventory(monkeypatch)

    import hermes_cli.profiles as profiles_mod

    def _boom():
        raise AssertionError("list_profiles must not be called from /lanes")

    monkeypatch.setattr(profiles_mod, "list_profiles", _boom)

    r = client.get("/api/plugins/kanban/lanes")
    assert r.status_code == 200, r.text
    data = r.json()
    catalog = {p["name"]: p for p in data["profiles"]}
    assert catalog["coder"]["worker_runtime"] == "claude-cli"
    assert catalog["coder"]["default_model"] == "claude-fable-5"
    assert catalog["coder"]["description"] == "builds things"
    assert catalog["research"]["worker_runtime"] == "hermes"
    assert catalog["research"]["default_model"] == "gpt-5.4"
    assert catalog["research"]["default_provider"] == "openrouter"
    assert catalog["research"]["fallback_providers"] == [
        {"provider": "openai-codex", "model": "gpt-5.5"},
    ]
    # The dynamic model catalog comes from shared inventory and still picks up
    # profile defaults even when uncatalogued for that provider.
    model_ids = {m["id"] for m in data["models"]}
    openrouter_rows = [m for m in data["models"] if m.get("provider") == "openrouter"]
    assert any(m["id"] == "qwen/qwen3.7-max" for m in openrouter_rows)
    assert "gpt-5.4" in model_ids
    assert "moonshotai/kimi-k2.7" in model_ids
    assert any(
        m["runtime"] == "claude-cli" and m["id"] == "claude-fable-5"
        for m in data["models"]
    )


def test_lanes_openrouter_import_smokes_then_persists_config(client, monkeypatch):
    import yaml

    mod = _plugin_module()
    calls: list[str] = []

    def fake_smoke(model_id: str) -> tuple[bool, str]:
        calls.append(model_id)
        if model_id == "vendor/fail":
            return False, "provider rejected model"
        return True, "Smoke ok"

    monkeypatch.setattr(mod, "_smoke_openrouter_model_id", fake_smoke)

    r = client.post(
        "/api/plugins/kanban/lanes/openrouter-models/import",
        json={
            "raw_text": (
                "xiaomi/mimo-v2.5\n"
                "bad-token\n"
                "openrouter:moonshotai/kimi-k2.7\n"
                "xiaomi/mimo-v2.5\n"
                "vendor/fail"
            )
        },
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert calls == ["xiaomi/mimo-v2.5", "moonshotai/kimi-k2.7", "vendor/fail"]
    assert data["admitted"] == ["xiaomi/mimo-v2.5", "moonshotai/kimi-k2.7"]
    assert {row["id"]: row["status"] for row in data["results"]} == {
        "xiaomi/mimo-v2.5": "admitted",
        "bad-token": "invalid",
        "moonshotai/kimi-k2.7": "admitted",
        "vendor/fail": "failed",
    }

    cfg_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["model_catalog"]["providers"]["openrouter"]["extra_models"] == [
        "xiaomi/mimo-v2.5",
        "moonshotai/kimi-k2.7",
    ]


def test_lanes_openrouter_import_reports_already_configured(client, monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "model_catalog:\n"
        "  providers:\n"
        "    openrouter:\n"
        "      extra_models:\n"
        "        - xiaomi/mimo-v2.5\n",
        encoding="utf-8",
    )
    mod = _plugin_module()
    monkeypatch.setattr(
        mod, "_smoke_openrouter_model_id", lambda model_id: (True, "Smoke ok")
    )

    r = client.post(
        "/api/plugins/kanban/lanes/openrouter-models/import",
        json={"raw_text": "xiaomi/mimo-v2.5"},
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["admitted"] == []
    assert data["configured"] == ["xiaomi/mimo-v2.5"]
    assert data["results"] == [
        {
            "id": "xiaomi/mimo-v2.5",
            "status": "already_configured",
            "reason": "Smoke ok; already present in config",
        }
    ]


def test_lanes_profile_catalog_cached_between_requests(client, monkeypatch):
    _write_lane_profiles(Path(os.environ["HERMES_HOME"]))
    mod = _plugin_module()
    mod._lane_profile_cache = None
    _stub_lane_inventory(monkeypatch)

    calls = {"n": 0}
    real_scan = mod._scan_lane_profiles

    def counting_scan():
        calls["n"] += 1
        return real_scan()

    monkeypatch.setattr(mod, "_scan_lane_profiles", counting_scan)
    assert client.get("/api/plugins/kanban/lanes").status_code == 200
    assert client.get("/api/plugins/kanban/lanes").status_code == 200
    assert calls["n"] == 1


def test_lanes_catalog_includes_kanban_spawn_health(client, monkeypatch):
    """GET /lanes muss pro Katalog-Profil kanban_spawn_health liefern — das
    Frontend (TriageStrip-Eskalation) blockt sonst jede Eskalation mit
    'keine Kanban-Spawn-Health im Lane-Katalog'."""
    _write_lane_profiles(Path(os.environ["HERMES_HOME"]))
    mod = _plugin_module()
    mod._lane_profile_cache = None
    _stub_lane_inventory(monkeypatch)
    monkeypatch.setattr(mod, "_claude_worker_available", lambda: True)

    r = client.get("/api/plugins/kanban/lanes")
    assert r.status_code == 200, r.text
    catalog = {p["name"]: p for p in r.json()["profiles"]}
    assert catalog["coder"]["kanban_spawn_health"]["status"] == "healthy"
    assert catalog["research"]["kanban_spawn_health"]["status"] == "healthy"


def test_lanes_catalog_spawn_health_unhealthy_without_claude_binary(
    client, monkeypatch
):
    _write_lane_profiles(Path(os.environ["HERMES_HOME"]))
    mod = _plugin_module()
    mod._lane_profile_cache = None
    _stub_lane_inventory(monkeypatch)
    monkeypatch.setattr(mod, "_claude_worker_available", lambda: False)

    r = client.get("/api/plugins/kanban/lanes")
    assert r.status_code == 200, r.text
    catalog = {p["name"]: p for p in r.json()["profiles"]}
    health = catalog["coder"]["kanban_spawn_health"]
    assert health["status"] == "unhealthy"
    assert "claude" in (health["reason"] or "")
    assert catalog["research"]["kanban_spawn_health"]["status"] == "healthy"


def test_lanes_spawn_check_reports_healthy_hermes_combo(client, monkeypatch):
    _write_lane_profiles(Path(os.environ["HERMES_HOME"]))
    mod = _plugin_module()
    mod._lane_profile_cache = None
    _stub_lane_inventory(monkeypatch)

    r = client.post(
        "/api/plugins/kanban/lanes/spawn-check",
        json={"profile": "research", "worker_runtime": "hermes", "model": "gpt-5.4"},
    )

    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "healthy",
        "reason": "Hermes worker profile is available",
        "dispatcher_path": "hermes",
        "resolved_model": "gpt-5.4",
    }


def test_lanes_spawn_check_rejects_obvious_model_runtime_mismatch(client):
    _write_lane_profiles(Path(os.environ["HERMES_HOME"]))
    mod = _plugin_module()
    mod._lane_profile_cache = None

    r = client.post(
        "/api/plugins/kanban/lanes/spawn-check",
        json={
            "profile": "research",
            "worker_runtime": "hermes",
            "model": "claude-fable-5",
        },
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "unhealthy"
    assert data["dispatcher_path"] == "hermes"
    assert data["resolved_model"] == "claude-fable-5"
    assert "belongs to claude-cli" in data["reason"]


def test_lanes_spawn_check_reports_unknown_profile(client):
    _write_lane_profiles(Path(os.environ["HERMES_HOME"]))
    mod = _plugin_module()
    mod._lane_profile_cache = None

    r = client.post(
        "/api/plugins/kanban/lanes/spawn-check",
        json={"profile": "ghost", "worker_runtime": "hermes", "model": "gpt-5.4"},
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "unhealthy"
    assert data["dispatcher_path"] == "hermes"
    assert "Profile 'ghost' is not in the lane catalog" in data["reason"]


# ---------------------------------------------------------------------------
# GET /planspecs/detail
# ---------------------------------------------------------------------------


def _write_open_planspec_fixture(plans_root: Path) -> Path:
    """A self-contained, valid, OPEN binding PlanSpec for detail-endpoint tests.

    Decoupled from real vault specs on purpose: a hardcoded real planspec
    legitimately transitions to a closed/shipped status as its work ships,
    at which point parse_binding_planspec rejects it (closed status) and a
    test pinned to it would flip red through no fault of the endpoint.
    """
    path = plans_root / "Claude-Code" / "plans" / "2026-06-18-detail-fixture.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
status: approved_for_ingest
topic: "Detail endpoint fixture"
freigabe: complete
live_test_depth: contract
acceptance_criteria:
  - id: AC-1
    statement: "The endpoint returns this acceptance criterion."
anti_scope:
  - "Nothing extra is in scope."
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: "First subtask"
      lane: coder
      deps: []
---

# Detail endpoint fixture

Body text.
""",
        encoding="utf-8",
    )
    return path


def _bind_detail_plans_root(monkeypatch, plans_root: Path):
    """Make the detail endpoint resolve+parse under ``plans_root`` (the endpoint
    calls parse_binding_planspec without a plans_root, which is otherwise bound
    to the real vault root via the function default)."""
    from hermes_cli import planspecs as _ps

    real_parse = _ps.parse_binding_planspec
    monkeypatch.setattr(
        _ps,
        "parse_binding_planspec",
        lambda p, **kw: real_parse(p, plans_root=plans_root),
    )


def test_planspecs_detail_happy_path(client, tmp_path, monkeypatch):
    """Happy path: a valid OPEN binding planspec → 200 with all fields populated."""
    plans_root = tmp_path / "vault" / "03-Agents"
    path = _write_open_planspec_fixture(plans_root)
    _bind_detail_plans_root(monkeypatch, plans_root)
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={"path": str(path)},
    )
    assert r.status_code == 200, r.text
    data = r.json()

    # Top-level fields present
    assert "goal" in data
    assert "freigabe" in data
    assert "live_test_depth" in data
    assert "anti_scope" in data
    assert "subtasks" in data

    # acceptance_criteria non-empty (this planspec has structured ACs)
    assert isinstance(data["acceptance_criteria"], list)
    assert len(data["acceptance_criteria"]) > 0, "expected at least one AC"

    # anti_scope non-empty
    assert isinstance(data["anti_scope"], list)
    assert len(data["anti_scope"]) > 0, "expected at least one anti_scope entry"

    # freigabe and live_test_depth populated
    assert data["freigabe"], "freigabe must be non-empty"
    assert data["live_test_depth"], "live_test_depth must be non-empty"

    # subtasks list with correct keys
    assert isinstance(data["subtasks"], list)
    assert len(data["subtasks"]) > 0, "expected at least one subtask"
    for st in data["subtasks"]:
        assert "id" in st, f"subtask missing id: {st}"
        assert "title" in st, f"subtask missing title: {st}"
        assert "lane" in st, f"subtask missing lane: {st}"
        assert "deps" in st, f"subtask missing deps: {st}"
        assert isinstance(st["deps"], list)


def test_planspecs_detail_traversal_relative(client):
    """Path traversal via relative ``../../..`` → 400, no file content leak."""
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={"path": "../../../../etc/passwd"},
    )
    assert r.status_code in (400, 404), r.text
    body = r.text
    # Must not echo /etc/passwd contents (e.g. "root:" marker)
    assert "root:" not in body, f"file content leaked in response: {body[:200]}"


def test_planspecs_detail_traversal_absolute(client):
    """Absolute path outside vault root → 400, no file content leak."""
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={"path": "/etc/passwd"},
    )
    assert r.status_code in (400, 404), r.text
    body = r.text
    assert "root:" not in body, f"file content leaked in response: {body[:200]}"


def test_planspecs_detail_missing_file_under_root(client):
    """Valid path under vault root but file does not exist → 404."""
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={
            "path": "/home/piet/vault/03-Agents/Claude-Code/plans/does-not-exist.md"
        },
    )
    assert r.status_code == 404, r.text


def test_planspecs_detail_non_md_suffix(client):
    """Path with non-.md suffix → 400."""
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={"path": "/home/piet/vault/03-Agents/Claude-Code/plans/something.txt"},
    )
    assert r.status_code == 400, r.text


def test_planspecs_detail_outside_root_does_not_leak_server_path(client):
    """#13: a path outside the vault root → 400 whose error must NOT disclose the
    server's configured absolute vault-root path (filesystem layout leak)."""
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={"path": "/etc/cron.d/evil.md"},
    )
    assert r.status_code == 400, r.text
    assert "/home/piet" not in r.text, f"server path leaked: {r.text[:300]}"
    assert "03-Agents" not in r.text, f"server path leaked: {r.text[:300]}"


def test_planspecs_detail_missing_file_does_not_leak_resolved_path(client):
    """#13: a missing file under root → 404 whose finding must not echo the
    resolved absolute server path."""
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={
            "path": "/home/piet/vault/03-Agents/Claude-Code/plans/does-not-exist.md"
        },
    )
    assert r.status_code == 404, r.text
    body = r.json()
    findings = body.get("detail", {}).get("findings", [])
    assert findings, body
    assert not any("/home/piet" in f for f in findings), (
        f"resolved path leaked: {findings}"
    )


def test_planspecs_detail_resolves_path_exactly_once(client, tmp_path, monkeypatch):
    """#13 (TOCTOU): the handler must resolve the path EXACTLY ONCE — inside
    parse_binding_planspec — not validate-then-re-resolve+read in two separate
    calls, which opens a window for a symlink swap between the two resolutions."""
    from hermes_cli import planspecs as _ps

    plans_root = tmp_path / "vault" / "03-Agents"
    path = _write_open_planspec_fixture(plans_root)
    _bind_detail_plans_root(monkeypatch, plans_root)

    calls = {"n": 0}
    real = _ps.resolve_planspec_path

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(_ps, "resolve_planspec_path", counting)
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={"path": str(path)},
    )
    assert r.status_code == 200, r.text
    assert calls["n"] == 1, (
        f"path resolved {calls['n']}× — TOCTOU window between resolutions"
    )


def test_planspecs_detail_null_byte_path_is_400_not_500(client):
    """An embedded NUL byte in ``path`` makes Path.resolve() raise ValueError.
    The handler must surface that as a 400 (malformed path), NOT let it become
    an unhandled 500, and must not leak the server path."""
    r = client.get(
        "/api/plugins/kanban/planspecs/detail",
        params={"path": "/home/piet/vault/03-Agents/x\x00.md"},
    )
    assert r.status_code == 400, r.text
    body = r.json()
    findings = body.get("detail", {}).get("findings", [])
    assert findings, body
    # path-free message: never echo the resolved server path / raw param
    assert not any("/home/piet" in f for f in findings), findings


def test_resolve_planspec_path_raises_typed_not_found():
    """The 404-vs-400 split must rest on a typed exception, not on substring-
    matching the human finding text. ``resolve_planspec_path`` raises
    ``PlanSpecNotFound`` (a ``PlanSpecBlocked`` subclass) for a missing file
    under root, and a *plain* ``PlanSpecBlocked`` for traversal — so a future
    wording change of the finding can't silently flip 404 → 400."""
    from hermes_cli import planspecs as _ps

    with pytest.raises(_ps.PlanSpecNotFound):
        _ps.resolve_planspec_path(
            "/home/piet/vault/03-Agents/Claude-Code/plans/does-not-exist-xyz.md"
        )

    with pytest.raises(_ps.PlanSpecBlocked) as exc_info:
        _ps.resolve_planspec_path("/etc/passwd")
    assert not isinstance(exc_info.value, _ps.PlanSpecNotFound), (
        "traversal/outside-root must be a 400-class block, not a not-found"
    )


# ---------------------------------------------------------------------------
# Phase4 C: GET /tasks/{id} surfaces planspec_source (card->spec 1-hop)
# ---------------------------------------------------------------------------


def test_get_task_surfaces_planspec_source_one_hop(client):
    """The card-detail endpoint exposes the task's own planspec_source so the
    drawer can resolve card->spec directly (no parent->root walk)."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "spec-sourced card", "assignee": "coder"},
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]

    spec_path = "/home/piet/vault/03-Agents/Claude-Code/plans/x.md"
    with kb.connect() as conn:
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id = ?", (spec_path, task_id)
        )
        conn.commit()

    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200, r.text
    assert r.json()["task"]["planspec_source"] == spec_path


def test_get_task_planspec_source_null_for_plain_task(client):
    """A non-PlanSpec task reports planspec_source = null (no 2-hop fallback)."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "plain card", "assignee": "coder"},
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200, r.text
    assert r.json()["task"]["planspec_source"] is None


# ---------------------------------------------------------------------------
# Round D: block_reason in board payload
# ---------------------------------------------------------------------------


def test_board_block_reason_operator_hold(client):
    """GET /board exposes block_reason='operator hold' for a held task.

    hold_task() is the only path that synthesises a run with summary='operator hold'.
    The board endpoint must surface that summary as block_reason so the UI can
    distinguish operator holds from other blocked causes.
    """
    # Create + claim (running) so hold_task has something to act on.
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "held task", "assignee": "coder"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]

    with kb.connect() as conn:
        kb.claim_task(conn, tid)
        held = kb.hold_task(conn, tid, reason="operator hold")
    assert held, "hold_task must return True for a running task"

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    data = r.json()
    blocked_col = next(c for c in data["columns"] if c["name"] == "blocked")
    task_card = next((t for t in blocked_col["tasks"] if t["id"] == tid), None)
    assert task_card is not None, "held task must appear in blocked column"
    assert task_card["block_reason"] == "operator hold", (
        f"expected 'operator hold', got {task_card.get('block_reason')!r}"
    )
    assert task_card["operator_question"] is True

    detail = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["task"]["operator_question"] is True


def test_board_block_reason_null_for_non_hold(client):
    """GET /board block_reason is null for a task blocked without hold_task.

    A task blocked via block_task (worker-side, e.g. 'review-required: ...') must
    NOT show block_reason='operator hold'. The UI must not show a Resume button.
    """
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "worker-blocked task", "assignee": "coder"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]

    with kb.connect() as conn:
        kb.claim_task(conn, tid)
        # block_task with a non-hold reason simulates worker hitting a wall.
        kb.block_task(conn, tid, reason="review-required: missing tests")

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    data = r.json()
    blocked_col = next(c for c in data["columns"] if c["name"] == "blocked")
    task_card = next((t for t in blocked_col["tasks"] if t["id"] == tid), None)
    assert task_card is not None
    # block_reason may be the worker reason string, but must NOT contain "operator hold"
    reason = task_card.get("block_reason") or ""
    assert "operator hold" not in reason.lower(), (
        f"non-hold blocked task must not have operator-hold block_reason, got {reason!r}"
    )


def test_board_verifier_question_is_not_an_operator_question(client):
    """A question mark in first-pass REQUEST_CHANGES prose stays retryable.

    The dashboard must consume the dispatcher's verdict-aware classification,
    not independently infer an operator action from punctuation.
    """
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "review feedback", "assignee": "coder", "body": "AC v1"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]

    with kb.connect() as conn:
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="Verifier asks: why is this assertion missing?")
        conn.execute(
            "UPDATE task_runs SET verdict = 'REQUEST_CHANGES' "
            "WHERE task_id = ? AND outcome = 'blocked'",
            (tid,),
        )
        conn.commit()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    blocked_col = next(c for c in r.json()["columns"] if c["name"] == "blocked")
    task_card = next(t for t in blocked_col["tasks"] if t["id"] == tid)
    assert task_card["block_reason"] == "Verifier asks: why is this assertion missing?"
    assert task_card["operator_question"] is False

    detail = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["task"]["operator_question"] is False


def test_release_gate_endpoint_requires_confirm_and_executes_blocked_gate(client, monkeypatch):
    conn = kb.connect()
    try:
        gate_id = kb.create_task(conn, title="release-gate: root", assignee="verifier")
        kb.block_task(conn, gate_id, reason="awaiting release-gate GO")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                gate_id,
                "release_gate_parked",
                {"root_id": "t_root", "reason": "awaiting release-gate GO"},
            )
    finally:
        conn.close()

    calls = []

    def fake_spawn(task_id, board=None):
        # The endpoint must LAUNCH the activation detached, never run it inline
        # (a synchronous restart would kill this very request — self-termination).
        calls.append(task_id)
        return {
            "ok": True,
            "unit": f"hermes-release-gate-{task_id}",
            "detail": "runtime activation started (detached); watch the release-gate task",
        }

    from hermes_cli import kanban_worktrees

    monkeypatch.setattr(kanban_worktrees, "spawn_release_gate_activation", fake_spawn)

    confirm_missing = client.post(
        f"/api/plugins/kanban/tasks/{gate_id}/release-gate", json={"confirm": False}
    )
    assert confirm_missing.status_code == 200
    assert confirm_missing.json()["ok"] is False
    assert calls == []

    response = client.post(
        f"/api/plugins/kanban/tasks/{gate_id}/release-gate", json={"confirm": True}
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "activating",
        "unit": f"hermes-release-gate-{gate_id}",
        "detail": "runtime activation started (detached); watch the release-gate task",
    }
    assert calls == [gate_id]


def test_release_gate_endpoint_rejects_non_blocked_gate(client):
    conn = kb.connect()
    try:
        gate_id = kb.create_task(conn, title="release-gate: root", assignee="verifier")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                gate_id,
                "release_gate_parked",
                {"root_id": "t_root", "reason": "awaiting release-gate GO"},
            )
    finally:
        conn.close()

    response = client.post(
        f"/api/plugins/kanban/tasks/{gate_id}/release-gate", json={"confirm": True}
    )
    assert response.status_code == 409
    assert "not blocked" in response.json()["detail"]
