"""Prompt-Schmiede endpoints — catalog (GET) + generate (POST)."""
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import promptforge_view


def _client() -> TestClient:
    app = FastAPI()
    promptforge_view.register_promptforge_routes(app)
    return TestClient(app)


def _install_fake_call_llm(monkeypatch, *, content=None, raises=None):
    """Inject a fake agent.auxiliary_client.call_llm so /generate never hits the network."""
    captured: dict = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        msg = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])

    # Ensure the parent package exists so `from agent.auxiliary_client import call_llm`
    # resolves to our fake without importing the heavy real module.
    if "agent" not in sys.modules:
        pkg = types.ModuleType("agent")
        pkg.__path__ = []  # mark as package
        monkeypatch.setitem(sys.modules, "agent", pkg)
    module = types.ModuleType("agent.auxiliary_client")
    module.call_llm = fake_call_llm
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    monkeypatch.setattr(sys.modules["agent"], "auxiliary_client", module, raising=False)
    return captured


def test_catalog_returns_200_and_full_schema():
    resp = _client().get("/api/promptforge/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1
    for key in ("blocks", "taskTypes", "modes", "targets", "heuristic", "evalEvidence"):
        assert isinstance(data[key], list) and data[key], f"{key} must be a non-empty list"
    assert len(data["blocks"]) == 12
    assert len(data["taskTypes"]) == 5
    assert len(data["modes"]) == 3
    assert len(data["targets"]) == 4
    assert len(data["heuristic"]) == 10
    assert len(data["evalEvidence"]) == 4


def test_blocks_have_required_fields():
    data = _client().get("/api/promptforge/catalog").json()
    for block in data["blocks"]:
        for field in ("id", "letter", "label", "description", "body", "source", "category"):
            assert block.get(field), f"block {block.get('id')} missing {field}"
        assert block["category"] in ("core", "long-run", "optional")


def test_task_types_reference_known_blocks():
    data = _client().get("/api/promptforge/catalog").json()
    known = {b["id"] for b in data["blocks"]}
    for tt in data["taskTypes"]:
        for field in ("id", "label", "blockIds", "typeBody", "defaultDoneWhen", "checklist", "rawTemplate", "source"):
            assert tt.get(field), f"taskType {tt.get('id')} missing {field}"
        for bid in tt["blockIds"]:
            assert bid in known, f"taskType {tt['id']} references unknown block {bid}"


def test_generate_returns_prompt_from_model(monkeypatch):
    captured = _install_fake_call_llm(monkeypatch, content="GENERATED PROMPT TEXT")
    resp = _client().post(
        "/api/promptforge/generate",
        json={"problem": "Backlog-Tab übersichtlicher machen", "targetId": "claude-goal", "taskTypeId": "feature", "modeId": "stop-on-doubt", "modelId": "claude-opus-4-8"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fallback"] is False
    assert data["prompt"] == "GENERATED PROMPT TEXT"
    # Cost guard: generator model is server-fixed to free Gemini Flash, NOT the caller's modelId.
    assert captured["provider"] == "gemini"
    assert captured["model"] == "gemini-3-flash-preview"
    assert captured["max_tokens"] == 1200
    # The caller's target model is only a hint in the system prompt, never the generator.
    system_msg = captured["messages"][0]["content"]
    assert "/goal" in system_msg.lower() or "transcript" in system_msg.lower()
    assert "claude-opus-4-8" in system_msg


def test_generate_empty_problem_is_fallback(monkeypatch):
    _install_fake_call_llm(monkeypatch, content="should not be used")
    data = _client().post("/api/promptforge/generate", json={"problem": "   "}).json()
    assert data["fallback"] is True
    assert data["prompt"] == ""


def test_generate_llm_failure_falls_back_gracefully(monkeypatch):
    _install_fake_call_llm(monkeypatch, raises=RuntimeError("provider down"))
    data = _client().post("/api/promptforge/generate", json={"problem": "do a thing"}).json()
    assert data["fallback"] is True
    assert data["prompt"] == ""
    assert "provider down" in data.get("error", "")
