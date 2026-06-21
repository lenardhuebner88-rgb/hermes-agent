"""Tests for the Strategist surface REST endpoints (G1).

Mirrors the bare-router harness of test_kanban_dashboard_plugin.py: the kanban
plugin router is attached to a stand-alone FastAPI app mounted at the real
prefix so we exercise the actual REST surface in isolation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import strategist_surface as ss

PREFIX = "/api/plugins/kanban"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_strategist_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix=PREFIX)
    return TestClient(app)


def _make_held_chain(*, annotate: bool = True) -> tuple[str, str]:
    """Create a held freigabe:operator root + one held child (decompose link
    direction). Returns (root_id, child_id)."""
    body = "Strategist lever proposal."
    if annotate:
        body += "\n\n" + ss.format_annotation(
            target_metric="Autonomie-% 62 → 75",
            roi="hoch",
            counter_metric="Fehl-Eskalations-Rate < 5%",
        )
    with kb.connect() as conn:
        root_id = kb.create_task(conn, title="Lever", body=body, assignee="coder-claude")
        child_id = kb.create_task(conn, title="Build lever", assignee="coder-claude")
        kb.link_tasks(conn, parent_id=child_id, child_id=root_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
                (root_id,),
            )
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (child_id,))
    return root_id, child_id


def _status(task_id: str) -> str:
    with kb.connect() as conn:
        return conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["status"]


# ---------------------------------------------------------------------------
# GET /strategist/proposals
# ---------------------------------------------------------------------------


def test_list_returns_held_proposals_with_annotations(client):
    root_id, _ = _make_held_chain(annotate=True)
    r = client.get(f"{PREFIX}/strategist/proposals")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    p = data["proposals"][0]
    assert p["id"] == root_id
    assert p["target_metric"] == "Autonomie-% 62 → 75"
    assert p["roi"] == "hoch"
    assert p["counter_metric"] == "Fehl-Eskalations-Rate < 5%"
    assert p["subtask_count"] == 1
    # No snapshot written yet → null, but the key is present as triage context.
    assert "metrics" in data and data["metrics"] is None


def test_list_includes_metric_snapshot_when_present(client):
    _make_held_chain()
    path = ss.vision_metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"autonomy_pct": 71, "green_gate_streak": 3}), encoding="utf-8")
    r = client.get(f"{PREFIX}/strategist/proposals")
    assert r.status_code == 200
    assert r.json()["metrics"] == {"autonomy_pct": 71, "green_gate_streak": 3}


def test_list_empty_when_no_held_proposals(client):
    r = client.get(f"{PREFIX}/strategist/proposals")
    assert r.status_code == 200
    data = r.json()
    assert data["proposals"] == []
    assert data["count"] == 0
    assert data["metrics"] is None
    assert isinstance(data["checked_at"], int)


def test_list_emits_weak_etag_and_304_roundtrip(client):
    _make_held_chain()
    r1 = client.get(f"{PREFIX}/strategist/proposals")
    etag = r1.headers.get("etag")
    assert etag and etag.startswith('W/"')
    assert r1.headers.get("cache-control") == "private, no-cache"
    r2 = client.get(f"{PREFIX}/strategist/proposals", headers={"If-None-Match": etag})
    assert r2.status_code == 304


# ---------------------------------------------------------------------------
# POST approve / veto
# ---------------------------------------------------------------------------


def test_approve_releases_held_chain(client):
    root_id, child_id = _make_held_chain()
    assert _status(root_id) == "scheduled"
    r = client.post(f"{PREFIX}/strategist/proposals/{root_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["released"] is True
    # Root left the hold; child is promoted out of 'scheduled'.
    assert _status(root_id) == "todo"
    assert _status(child_id) != "scheduled"


def test_veto_archives_root_and_children(client):
    root_id, child_id = _make_held_chain()
    r = client.post(f"{PREFIX}/strategist/proposals/{root_id}/veto")
    assert r.status_code == 200, r.text
    assert r.json()["vetoed"] is True
    assert _status(root_id) == "archived"
    assert _status(child_id) == "archived"
    # Vetoed proposal no longer appears in the list.
    listing = client.get(f"{PREFIX}/strategist/proposals").json()
    assert listing["count"] == 0


def test_approve_non_root_child_is_rejected(client):
    _root_id, child_id = _make_held_chain()
    # The held BUILD child carries no freigabe → not a proposal.
    r = client.post(f"{PREFIX}/strategist/proposals/{child_id}/approve")
    assert r.status_code == 409


def test_veto_non_root_child_is_rejected(client):
    _root_id, child_id = _make_held_chain()
    r = client.post(f"{PREFIX}/strategist/proposals/{child_id}/veto")
    assert r.status_code == 409
    # The child stays held — the guard refused to touch it.
    assert _status(child_id) == "scheduled"


def test_approve_unknown_task_is_rejected(client):
    r = client.post(f"{PREFIX}/strategist/proposals/t_does_not_exist/approve")
    assert r.status_code == 409


def test_veto_already_released_chain_is_rejected(client):
    root_id, _ = _make_held_chain()
    assert client.post(f"{PREFIX}/strategist/proposals/{root_id}/approve").status_code == 200
    # Once released (todo, building) a veto must NOT silently tear it down.
    r = client.post(f"{PREFIX}/strategist/proposals/{root_id}/veto")
    assert r.status_code == 409
    assert _status(root_id) == "todo"


# ---------------------------------------------------------------------------
# POST /tasks/{id}/veto-escalation — Autoresearch escalation veto (Naht 3)
# ---------------------------------------------------------------------------


def _make_autoresearch_escalation() -> str:
    from hermes_cli import autoresearch_reconcile as reconcile

    with kb.connect() as conn:
        return reconcile._escalate(
            conn,
            {
                "id": "p-esc",
                "finding_id": "p-esc",
                "title": "Autoresearch silent except",
                "mode": "code",
                "severity": "high",
                "subsystem": "auth",
                "theme": "silent-except",
                "status": "proposed",
            },
            reason="no diff, manual review",
        )


def test_veto_escalation_archives_and_returns_vetoed(client):
    task_id = _make_autoresearch_escalation()
    r = client.post(f"{PREFIX}/tasks/{task_id}/veto-escalation")
    assert r.status_code == 200, r.text
    assert r.json()["vetoed"] is True
    assert _status(task_id) == "archived"


def test_veto_escalation_on_plain_block_is_rejected(client):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="plain", assignee=None, initial_status="blocked", kind="ops"
        )
    r = client.post(f"{PREFIX}/tasks/{task_id}/veto-escalation")
    assert r.status_code == 409
    assert _status(task_id) == "blocked"


# ---------------------------------------------------------------------------
# POST run-propose / run-gutachter + GET run-status (manuelle Trigger, G1.5)
# Der echte _spawn_trigger wird gemockt, damit der Test KEINEN echten Strategen-/
# Gutachter-Lauf startet und nicht ins echte $HOME schreibt.
# ---------------------------------------------------------------------------


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_trigger_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeRunningProc:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode = None

    def poll(self):  # None = läuft noch
        return None


@pytest.fixture
def trigger_ctx(kanban_home):
    mod = _load_plugin_module()
    calls: list[str] = []

    def _fake_spawn(name: str):
        calls.append(name)
        proc = mod._TRIGGER_PROCS.get(name)
        if proc is not None and proc.poll() is None:
            return None  # echter Guard: läuft schon
        p = _FakeRunningProc()
        mod._TRIGGER_PROCS[name] = p
        return p

    mod._spawn_trigger = _fake_spawn
    app = FastAPI()
    app.include_router(mod.router, prefix=PREFIX)
    return mod, TestClient(app), calls


def test_run_propose_triggers_and_returns_pid(trigger_ctx):
    _mod, client, calls = trigger_ctx
    r = client.post(f"{PREFIX}/strategist/run-propose")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["pid"] == 4242 and body["name"] == "strategist-propose"
    assert calls == ["strategist-propose"]


def test_run_gutachter_triggers(trigger_ctx):
    _mod, client, _calls = trigger_ctx
    r = client.post(f"{PREFIX}/strategist/run-gutachter")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["name"] == "gutachter"


def test_double_trigger_is_guarded(trigger_ctx):
    _mod, client, _calls = trigger_ctx
    assert client.post(f"{PREFIX}/strategist/run-propose").json()["ok"] is True
    r2 = client.post(f"{PREFIX}/strategist/run-propose").json()
    assert r2["ok"] is False and r2["running"] is True


def test_run_status_shape(trigger_ctx):
    _mod, client, _calls = trigger_ctx
    data = client.get(f"{PREFIX}/strategist/run-status").json()
    for key in ("propose", "gutachter"):
        assert key in data
        assert set(data[key]) == {"running", "exit_code", "last_modified", "tail"}
        assert data[key]["running"] is False  # noch nichts gespawnt


def test_trigger_specs_argv_and_env(trigger_ctx):
    mod, _client, _calls = trigger_ctx
    pspec = mod._TRIGGER_SPECS["strategist-propose"]
    assert pspec["argv"][0] == "bash" and pspec["argv"][-1] == "propose"
    assert "strategist-cron.sh" in pspec["argv"][1]
    gspec = mod._TRIGGER_SPECS["gutachter"]
    assert gspec["argv"][-1].endswith("run.sh")
    assert gspec["env"]["DELIVER_MODE"] == "live"  # Phase-A live (Kommentar+Discord)
    # PATH wird angereichert, damit hermes/claude auflösen
    env = mod._trigger_env({})
    assert any(p.endswith("/.local/bin") for p in env["PATH"].split(":"))


def test_real_spawn_trigger_guard_blocks_when_running(kanban_home):
    """Der ECHTE _spawn_trigger (nicht gemockt) gibt None zurück, wenn schon ein
    Lauf aktiv ist — der Guard greift VOR jedem Popen, also ohne Seiteneffekt."""
    mod = _load_plugin_module()
    mod._TRIGGER_PROCS["gutachter"] = _FakeRunningProc()
    assert mod._spawn_trigger("gutachter") is None
