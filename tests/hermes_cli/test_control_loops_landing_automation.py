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
