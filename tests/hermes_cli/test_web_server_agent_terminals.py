from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import hermes_cli.web_server as web_server
from hermes_cli.agent_terminals import TmuxAgentSessionService


class FakeAgentTerminalService:
    def capabilities(self):
        return SimpleNamespace(to_dict=lambda: {"tmux_available": True, "hermes_tui_available": True, "hermes_binary": "/bin/hermes", "reason": None})

    def list_sessions(self):
        return ["work"]

    def list_windows(self, session=None):
        assert session in (None, "work")
        return [SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes", "active": True, "pane_id": "%1", "pid": 123, "command": "hermes"})]

    def show(self, session, window):
        assert (session, window) == ("work", "hermes")
        return SimpleNamespace(to_dict=lambda: {"session": session, "window": window})

    def ensure(self, kind, workdir=None):
        assert kind == "hermes"
        assert workdir in (None, "hermes-agent")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes"})

    def create_new(self, kind, workdir=None):
        assert kind == "hermes"
        assert workdir in (None, "hermes-agent")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes-2"})

    def respawn_dead(self, session, window):
        assert (session, window) == ("work", "hermes")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes"})

    def rename(self, session, window, name):
        assert (session, window, name) == ("work", "hermes", "hermes-renamed")
        return SimpleNamespace(to_dict=lambda: {"session": "work", "window": "hermes-renamed"})

    def kill_dead(self, session, window):
        assert (session, window) == ("work", "hermes")

    def terminate_live(self, session, window):
        assert (session, window) == ("work", "hermes")

    def capture(self, session, window, *, start=-200):
        assert (session, window, start) == ("work", "hermes", -10)
        return "captured"

    def attach_metadata(self, session, window):
        assert (session, window) == ("work", "hermes")
        return {"target": "work:hermes", "attach_argv": ["tmux", "attach-session", "-t", "work:hermes"]}

    def handoff_draft(self, session, window, *, start=-120):
        assert (session, window, start) == ("work", "hermes", -12)
        return {"target": "work:hermes", "content": "# handoff"}

    def send_keys(self, session, window, text):
        assert (session, window, text) == ("work", "hermes", "abc")

    def interrupt(self, session, window):
        assert (session, window) == ("work", "hermes")

    def detach_client(self, client_id):
        assert client_id == "client1"

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
    assert client.post("/api/agent-terminals/respawn", json={"session": "work", "window": "hermes"}, headers=headers).json()["window"]["window"] == "hermes"
    assert client.post("/api/agent-terminals/rename", json={"session": "work", "window": "hermes", "name": "hermes-renamed"}, headers=headers).json()["window"]["window"] == "hermes-renamed"
    assert client.post("/api/agent-terminals/kill-dead", json={"session": "work", "window": "hermes"}, headers=headers).json() == {"ok": True}
    assert client.post("/api/agent-terminals/terminate", json={"session": "work", "window": "hermes"}, headers=headers).json() == {"ok": True}
    assert client.post("/api/agent-terminals/capture", json={"session": "work", "window": "hermes", "start": -10}, headers=headers).json() == {"content": "captured"}
    assert client.post("/api/agent-terminals/attach-metadata", json={"session": "work", "window": "hermes"}, headers=headers).json()["metadata"]["target"] == "work:hermes"
    assert client.post("/api/agent-terminals/handoff-draft", json={"session": "work", "window": "hermes", "start": -12}, headers=headers).json()["draft"]["content"] == "# handoff"
    assert client.post("/api/agent-terminals/send-keys", json={"session": "work", "window": "hermes", "text": "abc"}, headers=headers).json() == {"ok": True}
    assert client.post("/api/agent-terminals/interrupt", json={"session": "work", "window": "hermes"}, headers=headers).json() == {"ok": True}
    assert client.post("/api/agent-terminals/detach-client", json={"client_id": "client1"}, headers=headers).json() == {"ok": True}

    schema = client.get("/openapi.json", headers=headers).json()
    names = {
        "AgentTerminalEnsureRequest",
        "AgentTerminalTargetRequest",
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


def test_agent_terminal_attach_uses_only_tmux_attach_argv(monkeypatch):
    class AttachService(FakeAgentTerminalService):
        def attach_argv(self, session, window):
            assert (session, window) == ("work", "hermes")
            return ["tmux", "-S", "/tmp/socket", "attach-session", "-t", "work:hermes"]

    spawned = {}

    class FakeBridge:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None):
            spawned["argv"] = argv
            spawned["cwd"] = cwd
            spawned["env"] = env
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


def test_agent_terminal_attach_consumes_resize_escape(monkeypatch):
    class AttachService(FakeAgentTerminalService):
        def attach_argv(self, session, window):
            return ["tmux", "attach-session", "-t", "work:hermes"]

    observed = {"writes": [], "resizes": []}

    class FakeBridge:
        @classmethod
        def spawn(cls, argv, cwd=None, env=None):
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
