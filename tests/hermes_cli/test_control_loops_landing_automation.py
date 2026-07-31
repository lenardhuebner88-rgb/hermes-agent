from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import control_loops
from hermes_cli.control_loops import (
    consume_landing_followup,
    get_landing_automation_enabled,
    register_landing_trigger,
    set_landing_automation_enabled,
)


def test_landing_automation_defaults_off_and_persists_audited_toggle(tmp_path):
    assert get_landing_automation_enabled(tmp_path) is False

    now = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)
    payload = set_landing_automation_enabled(
        True,
        updated_by="operator-test",
        state_dir=tmp_path,
        now=now,
    )

    assert payload == {
        "schema_version": 1,
        "enabled": True,
        "updated_at": now.isoformat(),
        "updated_by": "operator-test",
    }
    assert get_landing_automation_enabled(tmp_path) is True
    assert json.loads((tmp_path / "automation.json").read_text()) == payload


def test_collection_window_is_fixed_and_never_extended(tmp_path):
    set_landing_automation_enabled(True, updated_by="test", state_dir=tmp_path)
    opened_at = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)

    first = register_landing_trigger(
        "review:t_1", running=False, state_dir=tmp_path, now=opened_at
    )
    second = register_landing_trigger(
        "ready:t_2",
        running=False,
        state_dir=tmp_path,
        now=opened_at + timedelta(minutes=9),
    )

    assert first["reason"] == "window_opened"
    assert second["reason"] == "window_collected"
    assert first["collection_window"] == second["collection_window"]
    assert second["next_trigger_at"] == (opened_at + timedelta(minutes=10)).isoformat()


def test_running_signals_queue_at_most_one_followup(tmp_path):
    set_landing_automation_enabled(True, updated_by="test", state_dir=tmp_path)

    first = register_landing_trigger("ready:t_1", running=True, state_dir=tmp_path)
    second = register_landing_trigger("ready:t_2", running=True, state_dir=tmp_path)

    assert first["reason"] == "followup_queued"
    assert second["reason"] == "followup_already_queued"
    assert consume_landing_followup(tmp_path) is True
    assert consume_landing_followup(tmp_path) is False


def test_automation_off_suppresses_trigger_without_creating_event_state(tmp_path):
    result = register_landing_trigger("ready:t_1", running=False, state_dir=tmp_path)

    assert result == {"accepted": False, "reason": "automation_disabled"}
    assert not (tmp_path / "trigger-state.json").exists()


def test_control_api_exposes_and_toggles_landing_contract(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    landing = packs / "landing"
    landing.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (landing / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "landing",
                "type": "deterministic",
                "repo": str(repo),
                "description": "test landing",
                "stability": "experimental",
                "phases": {"run": {"command": "true", "timeout": 60}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_loops, "PACKS_DIR_OVERRIDE", packs)
    monkeypatch.setattr(control_loops, "STATE_ROOT_OVERRIDE", tmp_path / "state")
    monkeypatch.setattr(control_loops, "MODELS_PATH_OVERRIDE", tmp_path / "models.yaml")
    monkeypatch.setattr(
        control_loops,
        "_systemctl",
        lambda *args: subprocess.CompletedProcess(args, 1, stdout="", stderr=""),
    )
    app = FastAPI()
    control_loops.register_loops_routes(app)
    client = TestClient(app)

    listed = client.get("/api/loops").json()["packs"][0]
    detail = client.get("/api/loops/landing/detail").json()

    for payload in (listed, detail):
        assert payload["automation_enabled"] is False
        assert payload["baseline_sha"] is None
        assert payload["baseline_ok"] is None
        assert payload["baseline_reason"] is None
        assert payload["baseline_source"] is None
        assert payload["queue_summary"] == {}
        assert payload["next_trigger_at"] is None
        assert payload["last_result"] is None
        assert payload["collection_window"] is None
        assert payload["candidates"] == []

    enabled = client.put(
        "/api/loops/landing/automation", json={"enabled": True}
    ).json()
    assert enabled["enabled"] is True
    assert client.get("/api/loops/landing/detail").json()["automation_enabled"] is True


def test_landing_control_payload_whitelists_baseline_provenance(tmp_path):
    control_loops.write_landing_runtime_status(
        {
            "baseline_sha": "abc123def",
            "baseline_ok": False,
            "baseline_reason": "Kein grüner Nachweis für abc123def",
            "baseline_source": "none",
        },
        state_dir=tmp_path,
    )

    payload = control_loops._landing_control_payload(tmp_path)

    assert payload["baseline_reason"] == "Kein grüner Nachweis für abc123def"
    assert payload["baseline_source"] == "none"


# ── LL2-S5: Drawer-Detail-Payload + manuelle Vorschau (Dry-Run) ─────────────


def _landing_api_client(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    landing = packs / "landing"
    landing.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (landing / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "landing",
                "type": "deterministic",
                "repo": str(repo),
                "description": "test landing",
                "stability": "experimental",
                "phases": {"run": {"command": "true", "timeout": 60}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_loops, "PACKS_DIR_OVERRIDE", packs)
    monkeypatch.setattr(control_loops, "STATE_ROOT_OVERRIDE", tmp_path / "state")
    monkeypatch.setattr(control_loops, "MODELS_PATH_OVERRIDE", tmp_path / "models.yaml")
    monkeypatch.setattr(
        control_loops,
        "_systemctl",
        lambda *args: subprocess.CompletedProcess(args, 1, stdout="", stderr=""),
    )
    app = FastAPI()
    control_loops.register_loops_routes(app)
    return TestClient(app), tmp_path / "state" / "landing"


def test_landing_detail_exposes_s5_drawer_payload(tmp_path, monkeypatch):
    client, state = _landing_api_client(tmp_path, monkeypatch)

    detail = client.get("/api/loops/landing/detail").json()
    assert detail["gate_stages"] == ["baseline", "affected", "full", "post_merge"]
    assert detail["trigger_history"] == []
    assert detail["followup_pending"] is False
    assert detail["recovery"] == []
    assert detail["preview"] is None

    set_landing_automation_enabled(True, updated_by="test", state_dir=state)
    register_landing_trigger("review:t_1", running=False, state_dir=state)
    register_landing_trigger("ready:t_2", running=True, state_dir=state)

    detail = client.get("/api/loops/landing/detail").json()
    assert detail["trigger_history"] == ["review:t_1", "ready:t_2"]
    assert detail["followup_pending"] is True


def test_landing_recovery_entries_aggregate_per_fingerprint(tmp_path, monkeypatch):
    import sqlite3

    from hermes_cli import kanban_db

    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, kind TEXT NOT NULL,"
        " payload TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL)"
    )
    events = [
        ("t_1", "landing_recovery_requested",
         {"fingerprint": "fpA", "failure_class": "verify", "failing_gate": "affected",
          "candidate_commit": "cafe1", "requested_at": "2026-07-31T00:00:00+00:00"}),
        ("t_1", "landing_recovery_started",
         {"fingerprint": "fpA", "started_at": "2026-07-31T00:01:00+00:00"}),
        ("t_2", "landing_recovery_requested",
         {"fingerprint": "fpB", "failure_class": "gate", "requested_at": "2026-07-31T00:02:00+00:00"}),
        ("t_2", "landing_recovery_exhausted", {"fingerprint": "fpB"}),
        ("t_3", "unrelated_event", {"fingerprint": "fpC"}),
    ]
    for task_id, kind, payload in events:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, json.dumps(payload), 1785454320),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda *args, **kwargs: db_path)

    entries = control_loops._landing_recovery_entries()

    assert [(e["task_id"], e["state"]) for e in entries] == [
        ("t_2", "exhausted"),
        ("t_1", "started"),
    ]
    started = entries[1]
    assert started["fingerprint"] == "fpA"
    assert started["failing_gate"] == "affected"
    assert started["requested_at"] == "2026-07-31T00:00:00+00:00"
    assert started["started_at"] == "2026-07-31T00:01:00+00:00"


def test_landing_recovery_entries_fail_open_without_db(tmp_path, monkeypatch):
    from hermes_cli import kanban_db

    monkeypatch.setattr(
        kanban_db, "kanban_db_path", lambda *args, **kwargs: tmp_path / "missing.db"
    )
    assert control_loops._landing_recovery_entries() == []


def test_landing_preview_route_lifecycle(tmp_path, monkeypatch):
    client, state = _landing_api_client(tmp_path, monkeypatch)

    def fake_spawn(root):
        # Bewusst die AKTUELLE Zeit, nicht ein festes Datum: _landing_preview_status
        # bekommt hier keine Uhr injiziert, sondern vergleicht started_at gegen
        # datetime.now() und stuft alles aelter als LANDING_PREVIEW_STALE_SECONDS
        # (900 s) als verwaist ein. Ein fixer Zeitstempel laesst den Test genau bis
        # started_at + 900 s gruen sein und danach dauerhaft kippen — er war bei
        # Worker- und Verifier-Lauf gruen und beim Merge rot (2026-07-31).
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Spiegelt das mkdir aus _spawn_landing_preview (control_loops.py),
        # das im Fake sonst umgangen waere.
        root.mkdir(parents=True, exist_ok=True)
        (root / control_loops.LANDING_PREVIEW_STATE_FILENAME).write_text(
            json.dumps({"started_at": started_at}), encoding="utf-8"
        )
        return started_at

    monkeypatch.setattr(control_loops, "_spawn_landing_preview", fake_spawn)

    started = client.post("/api/loops/landing/preview")
    assert started.status_code == 200
    assert started.json()["ok"] is True

    running = client.get("/api/loops/landing/detail").json()["preview"]
    assert running["running"] is True
    assert running["finished_at"] is None

    conflict = client.post("/api/loops/landing/preview")
    assert conflict.status_code == 409

    (state / control_loops.LANDING_PREVIEW_OUTPUT_FILENAME).write_text(
        "Landing-Loop dry-run: 2 Kandidaten", encoding="utf-8"
    )
    (state / control_loops.LANDING_PREVIEW_DONE_FILENAME).write_text(
        json.dumps({"rc": 0, "finished_at": "2026-07-31T05:01:00+00:00"}),
        encoding="utf-8",
    )
    done = client.get("/api/loops/landing/detail").json()["preview"]
    assert done["running"] is False
    assert done["rc"] == 0
    assert "2 Kandidaten" in done["output_tail"]

    again = client.post("/api/loops/landing/preview")
    assert again.status_code == 200


def test_landing_preview_status_marks_stale_run_not_running(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    (tmp_path / control_loops.LANDING_PREVIEW_STATE_FILENAME).write_text(
        json.dumps({"started_at": old}), encoding="utf-8"
    )
    status = control_loops._landing_preview_status(tmp_path)
    assert status is not None
    assert status["running"] is False
