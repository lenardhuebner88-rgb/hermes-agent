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
    """autoresearch_view module with state + audit + home pointed at tmp_path."""
    state_dir = tmp_path / "runner-state"
    audit_dir = tmp_path / "audit"
    home = tmp_path / "home"
    state_dir.mkdir()
    audit_dir.mkdir()
    (home / "skills").mkdir(parents=True)
    (home / "config.yaml").write_text("model: MiniMax-M2.7-highspeed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("HERMES_HOME", str(home))
    module = importlib.import_module("hermes_cli.autoresearch_view")
    importlib.reload(module)
    module._state_dir_test = state_dir  # convenience handle for tests
    module._audit_dir_test = audit_dir
    module._home_test = home
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
# Trigger / Stop (no token; apply needs confirm; runner spawn is injected)
# --------------------------------------------------------------------------
@pytest.fixture()
def spawned(view, monkeypatch):
    calls = {}

    def fake_spawn(args):
        calls["args"] = args
        return 99999

    monkeypatch.setattr(view, "_spawn_runner", fake_spawn)
    return calls


def test_selftest_route_configured(client):
    cl, _view = client
    body = cl.get("/autoresearch/selftest").json()
    assert body["route_status"] == "configured"


def test_trigger_dry_run_spawns_without_apply(client, spawned):
    cl, view = client
    resp = cl.post("/autoresearch/trigger", json={"area": "all", "mode": "dry-run", "max_iterations": 2})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["ok"] and d["mode"] == "dry-run" and d["pid"] == 99999
    assert "--apply" not in spawned["args"]
    # a real run-request JSON was written into the (test) audit dir
    assert list((view._audit_dir_test / "run-requests").glob("*.json"))


def test_trigger_apply_requires_confirm(client, spawned):
    cl, _view = client
    resp = cl.post("/autoresearch/trigger", json={"area": "all", "mode": "apply", "confirm": False})
    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"]
    assert "args" not in spawned  # never spawned


def test_trigger_apply_with_confirm_passes_apply_flag(client, spawned):
    cl, _view = client
    resp = cl.post("/autoresearch/trigger", json={"area": "all", "mode": "apply", "confirm": True})
    assert resp.status_code == 200, resp.text
    assert "--apply" in spawned["args"] and "--confirm" in spawned["args"]


def test_trigger_rejects_bad_mode(client, spawned):
    cl, _view = client
    resp = cl.post("/autoresearch/trigger", json={"mode": "delete-everything"})
    assert resp.status_code == 400


def test_trigger_409_when_already_running(client, spawned):
    cl, view = client
    sd = view._state_dir_test
    _write(sd / "current.lock", {"pid": 4321, "request_id": "busy"})
    _write(sd / "current.heartbeat", {"ts": time.time()})
    resp = cl.post("/autoresearch/trigger", json={"area": "all", "mode": "dry-run"})
    assert resp.status_code == 409


def test_stop_idle_when_nothing_running(client):
    cl, _view = client
    d = cl.post("/autoresearch/stop").json()
    assert d["ok"] and d["state"] == "idle"


def test_stop_signals_lock_pid(client, view, monkeypatch):
    cl, v = client
    sent = {}
    monkeypatch.setattr(v, "_signal_pid", lambda pid, sig: sent.update(pid=pid, sig=sig))
    _write(v._state_dir_test / "current.lock", {"pid": 4321, "request_id": "r"})
    d = cl.post("/autoresearch/stop").json()
    assert d["ok"] and d["signalled"] == 4321 and sent["pid"] == 4321


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
    # read-only: no mutation forms in the served page (controls use fetch, not <form>)
    assert '<form' not in text.lower()
    assert 'method="post"' not in text.lower()


def test_html_view_has_operational_controls(client):
    cl, _view = client
    text = cl.get("/autoresearch").text
    for needle in ['id="pill"', 'id="btnDry"', 'id="btnApply"', 'id="btnStop"',
                   'id="iterbar"', 'id="weakness"', 'id="metrics"', 'id="results"',
                   'id="nextstep"', 'id="worklist"', 'id="lastrun"']:
        assert needle in text, f"missing {needle}"


def test_worklist_lists_open_scaffolds(client):
    cl, view = client
    sk = view._home_test / "skills" / "demo" / "x"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "# X\n\n## Output\n\n<!-- autoresearch-scaffold: replace with concrete guidance for `x` -->\n"
        "TODO: document the **Output** of `x`.\n",
        encoding="utf-8",
    )
    d = cl.get("/autoresearch/worklist").json()
    assert d["count"] == 1
    assert d["open_scaffolds"][0]["skill"] == "x"
    assert d["open_scaffolds"][0]["section"] == "Output"


def test_worklist_skips_archived(client):
    cl, view = client
    arch = view._home_test / "skills" / ".archive" / "y"
    arch.mkdir(parents=True)
    (arch / "SKILL.md").write_text(
        "# Y\n\n<!-- autoresearch-scaffold: x -->\nTODO: document the **Output** of `y`.\n",
        encoding="utf-8",
    )
    d = cl.get("/autoresearch/worklist").json()
    assert d["count"] == 0


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
