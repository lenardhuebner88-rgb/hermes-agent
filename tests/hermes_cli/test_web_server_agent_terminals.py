from __future__ import annotations

import shutil
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import hermes_cli.web_server as web_server
from hermes_cli import kanban_db as kb
from hermes_cli.agent_terminals import (
    AgentTerminalError,
    TmuxAgentSessionService,
    TmuxWindow,
)


class FakeAgentTerminalService:
    def capabilities(self):
        return SimpleNamespace(to_dict=lambda: {"tmux_available": True, "hermes_tui_available": True, "hermes_binary": "/bin/hermes", "reason": None})

    def cleanup_stale_isolated_attaches(self):
        return []

    def list_sessions(self):
        return ["work"]

    def list_windows(self, session=None):
        assert session in (None, "work")
        return [SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes", "active": True, "pane_id": "%1", "pid": 123, "command": "hermes"})]

    def show(self, session, window):
        assert (session, window) == ("work", "hermes")
        return SimpleNamespace(to_dict=lambda: {"session": session, "window": window})

    def ensure(self, kind, workdir=None, **kwargs):
        assert kind == "hermes"
        assert workdir in (None, "hermes-agent")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes"})

    def create_new(self, kind, workdir=None, **kwargs):
        assert kind == "hermes"
        assert workdir in (None, "hermes-agent")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes-2"})

    def respawn_dead(self, session, window, **kwargs):
        assert (session, window) == ("work", "hermes")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes"})

    def rename(self, session, window, name):
        assert (session, window, name) == ("work", "hermes", "hermes-renamed")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes-renamed"})

    def kill_dead(self, session, window):
        assert (session, window) == ("work", "hermes")

    def terminate_live(self, session, window, *, allow_external=False):
        assert (session, window) == ("work", "hermes")
        assert allow_external is False

    def capture(self, session, window, *, start=-200):
        assert (session, window, start) == ("work", "hermes", -10)
        return "captured"

    def attach_metadata(self, session, window):
        assert (session, window) == ("work", "hermes")
        return {"target": "work:hermes", "attach_argv": ["tmux", "attach-session", "-t", "work:hermes"]}

    def handoff_draft(self, session, window, *, start=-120):
        assert (session, window, start) == ("work", "hermes", -12)
        return {"schema_version": 1, "target": "work:hermes", "terminal_run_id": "tr_test", "upgrade_required": False, "capture": {"text": "captured", "encoding": "utf-8"}}

    def send_keys(self, session, window, text):
        assert (session, window, text) == ("work", "hermes", "abc")

    def interrupt(self, session, window):
        assert (session, window) == ("work", "hermes")

    def detach_client(self, client_id):
        assert client_id == "client1"

    def ensure_session_options(self, session):
        pass

    def overview(self, *, tail_lines=10):
        assert tail_lines == 10
        return {
            "now": 1751500000,
            "windows": [
                {
                    "session": "work",
                    "window": "hermes",
                    "active": True,
                    "pane_id": "%1",
                    "pid": 123,
                    "command": "hermes",
                    "cwd": "/home/user",
                    "dead": False,
                    "activity": 1751499990,
                    "tail": "─ ready │ gpt 5.5\n❯ Try \"write a test for…\"",
                    "state": "wartet",
                    "state_source": "heuristic",
                }
            ],
        }


class FakeExecutionCapsuleService:
    execution_server_id = "a" * 64

    def __init__(self, cwd: Path, *, stamp_error: Exception | None = None):
        self.cwd = cwd
        self.stamp_error = stamp_error
        self.correlation: dict[str, object | None] = {
            "task_id": None,
            "run_id": None,
            "correlation_id": None,
        }
        self.stamps: list[dict[str, object]] = []
        self.restores: list[dict[str, object | None]] = []

    def show(self, session: str, window: str) -> TmuxWindow:
        assert (session, window) == ("work", "codex")
        return TmuxWindow(
            session,
            window,
            True,
            "%7",
            123,
            "node",
            str(self.cwd),
            task_id=self.correlation["task_id"],
            run_id=self.correlation["run_id"],
            correlation_id=self.correlation["correlation_id"],
        )

    def stamp_execution_correlation(self, session, window, **kwargs):
        if self.stamp_error is not None:
            raise self.stamp_error
        assert kwargs["expected_pane_id"] == "%7"
        previous = dict(self.correlation)
        self.stamps.append(dict(kwargs))
        self.correlation = {
            "task_id": kwargs["task_id"],
            "run_id": kwargs["run_id"],
            "correlation_id": kwargs["correlation_id"],
        }
        return previous

    def restore_execution_correlation(
        self, session, window, previous, *, expected_pane_id
    ):
        assert (session, window, expected_pane_id) == ("work", "codex", "%7")
        self.restores.append(dict(previous))
        self.correlation = dict(previous)


@pytest.fixture
def execution_capsule_web_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True
    )
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="capsule API",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="main",
        )
        task = kb.claim_task(conn, task_id, claimer="api-test:1")
        assert task is not None and task.current_run_id is not None
        run_id = int(task.current_run_id)
    return repo, task_id, run_id


def _capsule_payload(task_id: str, run_id: int) -> dict:
    return {
        "session": "work",
        "window": "codex",
        "task_id": task_id,
        "run_id": run_id,
        "context_handoff": {
            "profile": "implementation",
            "summary": "Continue from verified state",
            "decisions": ["Keep task_runs authoritative"],
            "next_steps": ["Run the targeted gate"],
            "risks": ["No live activation"],
        },
    }


def test_agent_terminal_rest_routes_and_schemas_have_no_prompt_or_approval_fields(monkeypatch):
    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: FakeAgentTerminalService())
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    assert client.get("/api/agent-terminals/capabilities", headers=headers).json()["tmux_available"] is True
    assert client.get("/api/agent-terminals/sessions", headers=headers).json() == {"sessions": ["work"]}
    assert client.get("/api/agent-terminals/windows", params={"session": "work"}, headers=headers).json()["windows"][0]["window"] == "hermes"
    overview = client.get("/api/agent-terminals/overview", headers=headers).json()
    assert overview["now"] == 1751500000
    assert overview["windows"][0]["window"] == "hermes"
    assert overview["windows"][0]["state"] == "wartet"
    assert overview["windows"][0]["state_source"] == "heuristic"
    assert "ready" in overview["windows"][0]["tail"]
    assert client.post("/api/agent-terminals/show", json={"session": "work", "window": "hermes"}, headers=headers).json()["window"]["session"] == "work"
    assert client.post("/api/agent-terminals/ensure", json={"kind": "hermes"}, headers=headers).json()["window"]["window"] == "hermes"
    assert client.post("/api/agent-terminals/ensure", json={"kind": "hermes", "workdir": "hermes-agent"}, headers=headers).json()["window"]["window"] == "hermes"
    assert client.post("/api/agent-terminals/create", json={"kind": "hermes"}, headers=headers).json()["window"]["window"] == "hermes-2"
    assert client.post("/api/agent-terminals/create", json={"kind": "hermes", "workdir": "hermes-agent"}, headers=headers).json()["window"]["window"] == "hermes-2"
    rejected_identity = client.post(
        "/api/agent-terminals/create",
        json={
            "kind": "hermes",
            "native_session_id": "browser-controlled",
            "capsule_correlation_id": "0123456789abcdef01234567",
        },
        headers=headers,
    )
    assert rejected_identity.status_code == 422
    assert client.post("/api/agent-terminals/respawn", json={"session": "work", "window": "hermes"}, headers=headers).json()["window"]["window"] == "hermes"
    assert client.post("/api/agent-terminals/rename", json={"session": "work", "window": "hermes", "name": "hermes-renamed"}, headers=headers).json()["window"]["window"] == "hermes-renamed"
    assert client.post("/api/agent-terminals/kill-dead", json={"session": "work", "window": "hermes"}, headers=headers).json() == {"ok": True}
    assert client.post("/api/agent-terminals/terminate", json={"session": "work", "window": "hermes"}, headers=headers).json() == {"ok": True}
    assert client.post(
        "/api/agent-terminals/terminate",
        json={"session": "work", "window": "hermes", "external": False},
        headers=headers,
    ).json() == {"ok": True}
    assert client.post("/api/agent-terminals/capture", json={"session": "work", "window": "hermes", "start": -10}, headers=headers).json() == {"content": "captured"}
    assert client.post("/api/agent-terminals/attach-metadata", json={"session": "work", "window": "hermes"}, headers=headers).json()["metadata"]["target"] == "work:hermes"
    handoff = client.post("/api/agent-terminals/handoff-draft", json={"session": "work", "window": "hermes", "start": -12}, headers=headers).json()["draft"]
    assert handoff.get("schema_version") == 1
    assert "content" not in handoff
    assert handoff.get("terminal_run_id") == "tr_test"

    assert client.post("/api/agent-terminals/send-keys", json={"session": "work", "window": "hermes", "text": "abc"}, headers=headers).json() == {"ok": True}
    assert client.post("/api/agent-terminals/interrupt", json={"session": "work", "window": "hermes"}, headers=headers).json() == {"ok": True}
    assert client.post("/api/agent-terminals/detach-client", json={"client_id": "client1"}, headers=headers).json() == {"ok": True}

    schema = client.get("/openapi.json", headers=headers).json()
    names = {
        "AgentTerminalEnsureRequest",
        "AgentTerminalCreateRequest",
        "AgentTerminalTargetRequest",
        "AgentTerminalTerminateRequest",
        "AgentTerminalRenameRequest",
        "AgentTerminalCaptureRequest",
        "AgentTerminalHandoffDraftRequest",
        "AgentTerminalSendKeysRequest",
        "AgentTerminalDetachRequest",
    }
    schemas = schema["components"]["schemas"]
    forbidden = {"prompt", "permission_mode", "permissionMode", "auto_approval", "autoApproval", "approval"}
    for name in names:
        fields = set(schemas[name].get("properties", {}))
        assert fields.isdisjoint(forbidden), (name, fields)
    create_fields = set(
        schemas["AgentTerminalCreateRequest"].get("properties", {})
    )
    assert create_fields.isdisjoint(
        {"native_session_id", "capsule_correlation_id"}
    )
    assert "external" in schemas["AgentTerminalTerminateRequest"].get("properties", {})


def test_agent_terminal_sessions_maps_tmux_timeout_to_structured_503(monkeypatch):
    class TimeoutService(FakeAgentTerminalService):
        def cleanup_stale_isolated_attaches(self):
            raise AgentTerminalError("tmux command timed out: 10 seconds")

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: TimeoutService())
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    response = client.get("/api/agent-terminals/sessions", headers=headers)

    assert response.status_code == 503
    assert response.json() == {"detail": "tmux command timed out: 10 seconds"}


def test_execution_capsule_api_saga_binds_and_reads_consistent_generation(
    execution_capsule_web_env, monkeypatch: pytest.MonkeyPatch
):
    repo, task_id, run_id = execution_capsule_web_env
    service = FakeExecutionCapsuleService(repo)
    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: service)
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    response = client.post(
        "/api/agent-terminals/execution-capsule",
        json=_capsule_payload(task_id, run_id),
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capsule"]["state"] == "active"
    assert body["capsule"]["task_id"] == task_id
    assert body["capsule"]["run_id"] == run_id
    assert body["window"]["pane_id"] == "%7"
    assert body["window"]["correlation_id"] == body["capsule"]["correlation_id"]
    assert service.stamps == [
        {
            "expected_pane_id": "%7",
            "task_id": task_id,
            "run_id": run_id,
            "correlation_id": body["capsule"]["correlation_id"],
        }
    ]
    read = client.get(
        "/api/agent-terminals/execution-capsule",
        params={"session": "work", "window": "codex"},
        headers=headers,
    )
    assert read.status_code == 200, read.text
    assert read.json()["consistent"] is True
    assert read.json()["capsule"]["state"] == "active"


@pytest.mark.parametrize(
    "extra",
    [
        {"context_handoff": {"content": "raw pane output"}},
        {"workspace": "/browser/claimed/path"},
        {"commit": "browser-claimed-sha"},
        {"pane_id": "%99"},
        {"correlation_id": "browser-claimed"},
    ],
)
def test_execution_capsule_api_rejects_browser_claimed_identity_and_raw_content(
    execution_capsule_web_env, monkeypatch: pytest.MonkeyPatch, extra
):
    repo, task_id, run_id = execution_capsule_web_env
    service = FakeExecutionCapsuleService(repo)
    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: service)
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    payload = _capsule_payload(task_id, run_id)
    if "context_handoff" in extra:
        payload["context_handoff"].update(extra["context_handoff"])
    else:
        payload.update(extra)

    response = client.post(
        "/api/agent-terminals/execution-capsule", json=payload, headers=headers
    )

    assert response.status_code == 422
    assert service.stamps == []
    with kb.connect_closing() as conn:
        assert kb.get_execution_capsule(conn, run_id) is None


def test_execution_capsule_api_aborts_pending_binding_when_tmux_stamp_fails(
    execution_capsule_web_env, monkeypatch: pytest.MonkeyPatch
):
    repo, task_id, run_id = execution_capsule_web_env
    service = FakeExecutionCapsuleService(
        repo, stamp_error=AgentTerminalError("tmux stamp unavailable")
    )
    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: service)
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    response = client.post(
        "/api/agent-terminals/execution-capsule",
        json=_capsule_payload(task_id, run_id),
        headers=headers,
    )

    assert response.status_code == 503
    with kb.connect_closing() as conn:
        assert kb.get_execution_capsule(conn, run_id) is None
        events = kb.list_events(conn, task_id)
        assert any(event.kind == "execution_capsule_aborted" for event in events)


def test_execution_capsule_api_restores_tmux_when_run_ownership_changes_before_activation(
    execution_capsule_web_env, monkeypatch: pytest.MonkeyPatch
):
    repo, task_id, run_id = execution_capsule_web_env
    service = FakeExecutionCapsuleService(repo)
    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: service)
    real_activate = kb.activate_execution_capsule

    def lose_run_before_activate(conn, **kwargs):
        assert kb.complete_task(conn, task_id, expected_run_id=run_id)
        return real_activate(conn, **kwargs)

    monkeypatch.setattr(kb, "activate_execution_capsule", lose_run_before_activate)
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    response = client.post(
        "/api/agent-terminals/execution-capsule",
        json=_capsule_payload(task_id, run_id),
        headers=headers,
    )

    assert response.status_code == 409
    assert service.restores == [
        {"task_id": None, "run_id": None, "correlation_id": None}
    ]
    assert service.correlation == {
        "task_id": None,
        "run_id": None,
        "correlation_id": None,
    }
    with kb.connect_closing() as conn:
        assert kb.get_execution_capsule(conn, run_id) is None


def test_agent_terminal_terminate_external_flag_reaches_service(monkeypatch):
    """POST /terminate with external=true passes allow_external to the service."""
    calls: list[tuple[str, str, bool]] = []

    class ExternalTrackingService(FakeAgentTerminalService):
        def terminate_live(self, session, window, *, allow_external=False):
            calls.append((session, window, allow_external))

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: ExternalTrackingService())
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    assert client.post(
        "/api/agent-terminals/terminate",
        json={"session": "foreign", "window": "python3", "external": True},
        headers=headers,
    ).json() == {"ok": True}
    assert calls == [("foreign", "python3", True)]

    assert client.post(
        "/api/agent-terminals/terminate",
        json={"session": "work", "window": "hermes"},
        headers=headers,
    ).json() == {"ok": True}
    assert calls[-1] == ("work", "hermes", False)


def test_four_real_isolated_websockets_keep_distinct_same_session_windows(monkeypatch, tmp_path: Path):
    socket = tmp_path / "tmux.sock"
    service = TmuxAgentSessionService(socket_path=socket)
    windows = ["one", "two", "three", "four"]
    subprocess.run(["tmux", "-S", str(socket), "new-session", "-d", "-s", "work", "-n", windows[0], "sh", "-c", "while :; do sleep 60; done"], check=True)
    for window in windows[1:]:
        subprocess.run(["tmux", "-S", str(socket), "new-window", "-d", "-t", "work", "-n", window, "sh", "-c", "while :; do sleep 60; done"], check=True)

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: service)
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    try:
        client = TestClient(web_server.app)
        with ExitStack() as stack:
            sockets = [
                stack.enter_context(client.websocket_connect(f"/api/agent-terminals/attach?session=work&window={window}&isolated=1&client_id=probe-{index}"))
                for index, window in enumerate(windows)
            ]
            for index, websocket in enumerate(sockets):
                websocket.send_text(f"\x1b]777;RESIZE:{90 + index * 10}x{25 + index}\x07")
            time.sleep(0.25)
            rows = subprocess.run(
                ["tmux", "-S", str(socket), "list-clients", "-F", "#{session_name}|#{window_name}"],
                check=True, text=True, capture_output=True,
            ).stdout.splitlines()
            assert len(rows) == 4
            assert {row.split("|")[1] for row in rows} == set(windows)
            assert all(row.startswith("__hermes_attach_") for row in rows)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            internal = [name for name in subprocess.run(
                ["tmux", "-S", str(socket), "list-sessions", "-F", "#{session_name}"],
                check=True, text=True, capture_output=True,
            ).stdout.splitlines() if name.startswith("__hermes_attach_")]
            if not internal:
                break
            time.sleep(0.05)
        assert internal == []
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_agent_terminal_attach_uses_only_tmux_attach_argv(monkeypatch):
    class AttachService(FakeAgentTerminalService):
        def attach_argv(self, session, window):
            assert (session, window) == ("work", "hermes")
            return ["tmux", "-S", "/tmp/socket", "attach-session", "-t", "work:hermes"]

    spawned = {}

    class FakeBridge:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None, cols=80, rows=24):
            spawned["argv"] = argv
            spawned["cwd"] = cwd
            spawned["env"] = env
            spawned["cols"] = cols
            spawned["rows"] = rows
            return cls()

        def read(self, timeout):
            return None

        def write(self, raw):
            spawned["write"] = raw

        def close(self):
            spawned["closed"] = True

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: AttachService())
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "PtyBridge", FakeBridge)
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes"):
        pass

    assert spawned["argv"] == ["tmux", "-S", "/tmp/socket", "attach-session", "-t", "work:hermes"]
    assert spawned["env"] is None


def test_agent_terminal_isolated_attach_uses_group_and_always_cleans_up(monkeypatch):
    events: list[object] = []

    class AttachService(FakeAgentTerminalService):
        def create_isolated_attach(self, session, window):
            events.append(("create", session, window))
            return SimpleNamespace(session="__hermes_attach_test", window=window)

        def attach_argv(self, session, window):
            events.append(("argv", session, window))
            return ["tmux", "attach-session", "-t", f"{session}:{window}"]

        def cleanup_isolated_attach(self, session):
            events.append(("cleanup", session))
            return True

    class FakeBridge:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None, cols=80, rows=24):
            events.append(("spawn", tuple(argv)))
            return cls()

        def read(self, timeout):
            return None

        def write(self, raw):
            pass

        def close(self):
            events.append("close")

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: AttachService())
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "PtyBridge", FakeBridge)
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes&isolated=1"):
        pass

    assert events[:3] == [
        ("create", "work", "hermes"),
        ("argv", "__hermes_attach_test", "hermes"),
        ("spawn", ("tmux", "attach-session", "-t", "__hermes_attach_test:hermes")),
    ]
    assert events[-1] == ("cleanup", "__hermes_attach_test")
    assert events[3:-1] and all(event == "close" for event in events[3:-1])


def test_agent_terminal_isolated_attach_cleans_group_when_argv_validation_fails(monkeypatch):
    events: list[object] = []

    class InvalidAttachService(FakeAgentTerminalService):
        def create_isolated_attach(self, session, window):
            events.append(("create", session, window))
            return SimpleNamespace(session="__hermes_attach_invalid", window=window)

        def attach_argv(self, session, window):
            raise AgentTerminalError("target disappeared")

        def cleanup_isolated_attach(self, session):
            events.append(("cleanup", session))
            return True

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: InvalidAttachService())
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes&isolated=1") as websocket:
        assert "Invalid tmux target" in websocket.receive_text()
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()

    assert events == [
        ("create", "work", "hermes"),
        ("cleanup", "__hermes_attach_invalid"),
    ]


# Live incident: Handy = 69 Spalten, Höhen 35 (Keyboard offen) / 49 (Keyboard zu).
# Diese Fixture-Werte kommen direkt aus dem beobachteten Resize-Storm (2026-07-03).
def _make_attach_bridge_class(spawned: dict):
    """Return a FakeBridge that records spawn kwargs and immediately ends the read loop."""

    class FakeBridge:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None, cols=80, rows=24):
            spawned["argv"] = argv
            spawned["cols"] = cols
            spawned["rows"] = rows
            return cls()

        def read(self, timeout):
            return None

        def write(self, raw):
            pass

        def close(self):
            pass

    return FakeBridge


def _setup_attach_monkeypatches(monkeypatch, spawned: dict):
    class AttachService(FakeAgentTerminalService):
        def attach_argv(self, session, window):
            return ["tmux", "attach-session", "-t", f"{session}:{window}"]

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: AttachService())
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "PtyBridge", _make_attach_bridge_class(spawned))
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)


def test_agent_terminal_attach_ensures_session_options_before_spawn(monkeypatch):
    """WS attach must best-effort `mouse on`/history-limit an already-existing
    session on every reattach — not just at window-spawn time."""
    calls: list[str] = []

    class AttachService(FakeAgentTerminalService):
        def attach_argv(self, session, window):
            return ["tmux", "attach-session", "-t", f"{session}:{window}"]

        def ensure_session_options(self, session):
            calls.append(session)

    spawned: dict = {}
    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: AttachService())
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "PtyBridge", _make_attach_bridge_class(spawned))
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes"):
        pass

    assert calls == ["work"]


def test_agent_terminal_attach_spawns_with_client_dimensions(monkeypatch):
    """Attach with ?cols=69&rows=49 → PTY spawned at mobile size (live incident fixture)."""
    spawned: dict = {}
    _setup_attach_monkeypatches(monkeypatch, spawned)
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes&cols=69&rows=49"):
        pass
    assert spawned["cols"] == 69
    assert spawned["rows"] == 49


def test_agent_terminal_attach_spawns_with_defaults_when_no_params(monkeypatch):
    """Attach without cols/rows → PTY spawned at 80×24 default."""
    spawned: dict = {}
    _setup_attach_monkeypatches(monkeypatch, spawned)
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes"):
        pass
    assert spawned["cols"] == 80
    assert spawned["rows"] == 24


def test_agent_terminal_attach_garbage_dimensions_fall_back_to_default(monkeypatch):
    """cols=abc / rows=0 are invalid → fallback to 80×24, no exception."""
    spawned: dict = {}
    _setup_attach_monkeypatches(monkeypatch, spawned)
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes&cols=abc&rows=0"):
        pass
    assert spawned["cols"] == 80
    assert spawned["rows"] == 24


def test_agent_terminal_attach_consumes_resize_escape(monkeypatch):
    class AttachService(FakeAgentTerminalService):
        def attach_argv(self, session, window):
            return ["tmux", "attach-session", "-t", "work:hermes"]

    observed = {"writes": [], "resizes": []}

    class FakeBridge:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None, cols=80, rows=24):
            return cls()

        def read(self, timeout):
            time.sleep(0.02)
            return b""

        def write(self, raw):
            observed["writes"].append(raw)

        def resize(self, cols, rows):
            observed["resizes"].append((cols, rows))

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: AttachService())
    monkeypatch.setattr(web_server, "_PTY_BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(web_server, "PtyBridge", FakeBridge)
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes") as ws:
        ws.send_text("\x1b[RESIZE:47;31]")
        ws.send_text("real-input")
        time.sleep(0.1)

    assert observed["resizes"] == [(47, 31)]
    assert observed["writes"] == [b"real-input"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_agent_terminal_attach_disconnect_reaps_only_attach_client(monkeypatch, tmp_path: Path):
    socket = tmp_path / "tmux.sock"
    service = TmuxAgentSessionService(socket_path=socket, hermes_home=tmp_path)
    subprocess.run(
        ["tmux", "-S", str(socket), "new-session", "-d", "-s", "work", "-n", "hermes", "cat"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    monkeypatch.setattr(web_server, "_agent_terminal_service", lambda: service)
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    client = TestClient(web_server.app)
    try:
        with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes") as ws:
            ws.send_text("hello-through-attach\n")
            time.sleep(0.2)

        assert service.window_exists("work", "hermes")

        with client.websocket_connect("/api/agent-terminals/attach?session=work&window=hermes"):
            time.sleep(0.1)
        assert "hello-through-attach" in service.capture("work", "hermes", start=-20)
        assert service.window_exists("work", "hermes")
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
