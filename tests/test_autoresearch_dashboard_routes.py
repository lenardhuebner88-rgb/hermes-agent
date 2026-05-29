"""Phase 4 tests: read-only /autoresearch view + token-gated POST stubs.

These exercise the route layer in isolation by registering the routes on a
minimal FastAPI app (no heavy web_server import), plus unit tests on the pure
status/audit helpers with an env-overridden state dir.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]


def _load_stub():
    spec = importlib.util.spec_from_file_location(
        "autoresearch_heartbeat_stub", _ROOT / "scripts" / "autoresearch_heartbeat_stub.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def view(monkeypatch, tmp_path):
    """autoresearch_view module with state + audit dirs pointed at tmp_path."""
    state_dir = tmp_path / "runner-state"
    audit_dir = tmp_path / "audit"
    state_dir.mkdir()
    audit_dir.mkdir()
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(audit_dir))
    monkeypatch.delenv("HERMES_AUTORESEARCH_TOKEN", raising=False)
    module = importlib.import_module("hermes_cli.autoresearch_view")
    importlib.reload(module)
    module._state_dir_test = state_dir  # convenience handle for tests
    module._audit_dir_test = audit_dir
    return module


@pytest.fixture()
def client(view):
    app = FastAPI()
    view.register_autoresearch_routes(app)
    return TestClient(app), view


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# Status state machine
# --------------------------------------------------------------------------
def test_status_idle_when_no_lock(client):
    cl, _view = client
    resp = cl.get("/autoresearch/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    assert body["pid"] is None
    # route is configured by default (MiniMax-M2.7 present in config.yaml)
    assert body["route_status"] == "configured"


def test_status_running_with_fresh_lock_and_heartbeat(client):
    cl, view = client
    sd = view._state_dir_test
    _write(sd / "current.lock", {"pid": 4321, "request_id": "req-abc", "started_at": "2026-05-29T06:00:00Z"})
    _write(sd / "current.heartbeat", {
        "pid": 4321, "request_id": "req-abc",
        "iteration": 2, "max": 5, "last_step": "apply", "last_eval": "keep",
        "ts": time.time(),
    })
    body = cl.get("/autoresearch/status").json()
    assert body["state"] == "running"
    assert body["pid"] == 4321
    assert body["request_id"] == "req-abc"
    assert body["iteration"] == 2
    assert body["max"] == 5
    assert body["last_eval"] == "keep"
    assert body["heartbeat_fresh"] is True


def test_status_crashed_with_stale_heartbeat(client):
    cl, view = client
    sd = view._state_dir_test
    _write(sd / "current.lock", {"pid": 4321, "request_id": "req-stale", "started_at": "x"})
    _write(sd / "current.heartbeat", {
        "pid": 4321, "request_id": "req-stale",
        "iteration": 1, "max": 5, "last_step": "eval", "last_eval": "keep",
        "ts": time.time() - 99999,  # very old
    })
    body = cl.get("/autoresearch/status").json()
    assert body["state"] == "crashed"
    assert body["heartbeat_fresh"] is False


def test_status_stopping_when_status_declares_stopping(client):
    cl, view = client
    sd = view._state_dir_test
    _write(sd / "current.lock", {"pid": 7, "request_id": "req-stop", "started_at": "x"})
    _write(sd / "current.heartbeat", {"pid": 7, "request_id": "req-stop", "iteration": 3, "max": 5, "ts": time.time()})
    _write(sd / "current.status", {"state": "stopping", "route_status": "configured"})
    body = cl.get("/autoresearch/status").json()
    assert body["state"] == "stopping"


# --------------------------------------------------------------------------
# Token gate on the mutate routes
# --------------------------------------------------------------------------
def test_trigger_denied_without_token(client):
    cl, _view = client
    resp = cl.post("/autoresearch/trigger", json={"request_id": "x", "mode": "dry-run"})
    assert resp.status_code == 403


def test_stop_denied_without_token(client):
    cl, _view = client
    resp = cl.post("/autoresearch/stop")
    assert resp.status_code == 403


def test_trigger_denied_with_wrong_token(client, monkeypatch):
    cl, _view = client
    monkeypatch.setenv("HERMES_AUTORESEARCH_TOKEN", "the-real-token")
    resp = cl.post("/autoresearch/trigger", headers={"X-Autoresearch-Token": "wrong"})
    assert resp.status_code == 403


def test_trigger_with_valid_token_is_503_no_runner_in_phase4(client, monkeypatch):
    """Even a valid token must not execute anything in Phase 4 (no runner)."""
    cl, _view = client
    monkeypatch.setenv("HERMES_AUTORESEARCH_TOKEN", "the-real-token")
    resp = cl.post("/autoresearch/trigger", headers={"X-Autoresearch-Token": "the-real-token"})
    assert resp.status_code == 503
    assert "Phase 5" in resp.json()["detail"]


def test_stop_with_valid_token_is_503(client, monkeypatch):
    cl, _view = client
    monkeypatch.setenv("HERMES_AUTORESEARCH_TOKEN", "tok")
    resp = cl.post("/autoresearch/stop", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 503


# --------------------------------------------------------------------------
# Audit route
# --------------------------------------------------------------------------
def test_audit_parses_results_tsv(client):
    cl, view = client
    ad = view._audit_dir_test
    (ad / "autoresearch_results.tsv").write_text(
        "timestamp\tmode\ttarget\thypothesis\tdecision\trisk\tevidence\n"
        "2026-05-29T06:00:00Z\tskills\tgithub\tadd output contract\tkeep\tlow\teval pass\n"
        "2026-05-29T06:05:00Z\tskills\tdevops\ttighten safety gate\tblocked\tmed\troute yellow\n",
        encoding="utf-8",
    )
    body = cl.get("/autoresearch/audit").json()
    assert body["results_count"] == 2
    assert body["decision_counts"]["keep"] == 1
    assert body["decision_counts"]["blocked"] == 1
    assert body["results"][0]["target"] == "github"


def test_audit_empty_when_no_files(client):
    cl, _view = client
    body = cl.get("/autoresearch/audit").json()
    assert body["results_count"] == 0
    assert body["results"] == []
    assert body["inventory"] is None


# --------------------------------------------------------------------------
# HTML view
# --------------------------------------------------------------------------
def test_html_view_is_readonly_and_served(client):
    cl, _view = client
    resp = cl.get("/autoresearch")
    assert resp.status_code == 200
    text = resp.text
    assert "Hermes Autoresearch" in text
    # read-only: no mutation forms in the served page
    assert '<form' not in text.lower()
    assert 'method="post"' not in text.lower()


# --------------------------------------------------------------------------
# Heartbeat stub integration (drives the status route)
# --------------------------------------------------------------------------
def test_heartbeat_stub_drives_running_then_clear(client, monkeypatch):
    cl, view = client
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(view._state_dir_test))
    stub = _load_stub()
    stub.write_heartbeat(
        state_dir=view._state_dir_test,
        request_id="stub-1", iteration=2, max_iterations=5,
        last_step="eval", last_eval="keep",
    )
    assert cl.get("/autoresearch/status").json()["state"] == "running"
    stub.clear(view._state_dir_test)
    assert cl.get("/autoresearch/status").json()["state"] == "idle"
