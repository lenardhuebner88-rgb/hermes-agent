"""Contract tests for /home/piet/.hermes/bin/hub_watcher_consumer.py.

Phase-1 consumer must stay side-effect safe by default: dry-run classifies only;
--apply is the only path that appends processed markers / state.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


CONSUMER = Path("/home/piet/.hermes/bin/hub_watcher_consumer.py")


def _load_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not CONSUMER.exists():
        pytest.skip(f"local ops script not present: {CONSUMER}")
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    spec = importlib.util.spec_from_file_location("hub_watcher_consumer_under_test", str(CONSUMER))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    assert mod.HERMES_ROOT == hermes_home
    assert mod.LOG_FILE == hermes_home / "logs" / "hub-watcher-consumer.jsonl"
    return mod


def _ts(hours_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _write_jsonl(path: Path, rows: list[dict] | None = None, extra_lines: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows or []:
            fh.write(json.dumps(row) + "\n")
        for line in extra_lines or []:
            fh.write(line + "\n")


def _make_db(path: Path, *, task_id: str = "T1", status: str = "running") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL, "
            "priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER, "
            "completed_at INTEGER, last_heartbeat_at INTEGER, current_run_id INTEGER)"
        )
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)")
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at) VALUES (?, 'Task', ?, 0, 1)",
            (task_id, status),
        )
        conn.execute("INSERT INTO task_events (id, task_id, kind, created_at) VALUES (7, ?, 'note', 1)", (task_id,))
        conn.commit()
    finally:
        conn.close()


def test_policy_loads_external_yaml(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\nprofiles:\n  custom: {action: custom_action, cooldown_s: 12, requires_go: true}\n",
        encoding="utf-8",
    )
    loaded = mod.load_policy(policy)
    rule = mod.policy_for({"notifier_profile": "custom"}, loaded)
    assert rule["action"] == "custom_action"
    assert rule["cooldown_s"] == 12
    assert rule["requires_go"] is True


def test_processed_ref_is_skipped(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    pending = tmp_path / "pending.jsonl"
    original = {"ts": _ts(), "task_id": "T1", "notifier_profile": "default", "to_event_id": 5}
    marker = {"ts": _ts(), "processed_at": _ts(), "processed_ref": original["ts"]}
    _write_jsonl(pending, [original, marker])
    summary = mod.run_once(pending_path=pending, policy_path=tmp_path / "missing.yaml", state_path=tmp_path / "state.json", db_path=tmp_path / "missing.db")
    assert summary["decisions_count"] == 0


def test_stale_record_is_skipped(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    pending = tmp_path / "pending.jsonl"
    _write_jsonl(pending, [{"ts": _ts(hours_ago=30), "task_id": "T1", "notifier_profile": "default", "to_event_id": 5}])
    summary = mod.run_once(pending_path=pending, policy_path=tmp_path / "missing.yaml", state_path=tmp_path / "state.json", db_path=tmp_path / "missing.db")
    assert summary["decisions_count"] == 0


def test_cooldown_suppresses_repeated_action(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    pending = tmp_path / "pending.jsonl"
    state = tmp_path / "state.json"
    rec = {"ts": _ts(), "task_id": "T1", "notifier_profile": "hub:coord-stuck-60s", "to_event_id": 5}
    _write_jsonl(pending, [rec])
    state.write_text(
        json.dumps({"version": 1, "decisions": {"T1|hub:coord-stuck-60s|operator_alert_needed": {"last_decision_ts": _ts(), "count": 1}}}),
        encoding="utf-8",
    )
    summary = mod.run_once(pending_path=pending, policy_path=tmp_path / "missing.yaml", state_path=state, db_path=tmp_path / "missing.db")
    assert summary["decisions_count"] == 1
    assert summary["decisions"][0]["decision"] == "suppressed"
    assert summary["decisions"][0]["suppressed_by"].startswith("cooldown:")


def test_corrupt_and_non_object_jsonl_are_logged_and_tick_continues(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    pending = tmp_path / "pending.jsonl"
    _write_jsonl(
        pending,
        [{"ts": _ts(), "task_id": "T1", "notifier_profile": "default", "to_event_id": 5}],
        extra_lines=["{broken-json", "[]"],
    )
    summary = mod.run_once(pending_path=pending, policy_path=tmp_path / "missing.yaml", state_path=tmp_path / "state.json", db_path=tmp_path / "missing.db")
    assert summary["decisions_count"] == 1
    assert {e["error"] for e in summary["parse_errors"]} == {"json_decode", "non_object"}


def test_terminal_task_auto_closes(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    pending = tmp_path / "pending.jsonl"
    db = tmp_path / "kanban.db"
    _make_db(db, task_id="T1", status="done")
    _write_jsonl(pending, [{"ts": _ts(), "task_id": "T1", "notifier_profile": "default", "to_event_id": 5}])
    summary = mod.run_once(pending_path=pending, policy_path=tmp_path / "missing.yaml", state_path=tmp_path / "state.json", db_path=db)
    assert summary["decisions_count"] == 1
    assert summary["decisions"][0]["decision"] == "auto_closed_terminal"


def test_dry_run_has_no_side_effects_but_apply_appends_marker_and_state(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    pending = tmp_path / "pending.jsonl"
    state = tmp_path / "state.json"
    rec = {"ts": _ts(), "task_id": "T1", "notifier_profile": "default", "to_event_id": 5}
    _write_jsonl(pending, [rec])

    dry = mod.run_once(pending_path=pending, policy_path=tmp_path / "missing.yaml", state_path=state, db_path=tmp_path / "missing.db", apply=False)
    assert dry["decisions_count"] == 1
    assert len(pending.read_text().splitlines()) == 1
    assert not state.exists()
    assert not mod.LOG_FILE.exists()

    applied = mod.run_once(pending_path=pending, policy_path=tmp_path / "missing.yaml", state_path=state, db_path=tmp_path / "missing.db", apply=True)
    assert applied["decisions_count"] == 1
    assert "vault_stub_writes" not in applied
    assert not (tmp_path / "03-Agents" / "Hermes" / "_proposed").exists()
    rows = [json.loads(line) for line in pending.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[-1]["processed_ref"] == rec["ts"]
    assert state.exists()
    assert mod.LOG_FILE.exists()
    assert mod.LOG_FILE.read_text(encoding="utf-8").count("\n") == 1


def test_apply_writes_phase2_stub_only_to_explicit_tmp_vault_root(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    pending = tmp_path / "pending.jsonl"
    state = tmp_path / "state.json"
    vault_root = tmp_path / "vault"
    rec = {"ts": _ts(), "task_id": "T-Stub/1", "notifier_profile": "coordinator:hub-plan-ready", "to_event_id": 42}
    _write_jsonl(pending, [rec])

    applied = mod.run_once(
        pending_path=pending,
        policy_path=tmp_path / "missing.yaml",
        state_path=state,
        db_path=tmp_path / "missing.db",
        apply=True,
        vault_root=vault_root,
    )

    assert applied["decisions_count"] == 1
    assert applied["decisions"][0]["action"] == "evidence_stub_later"
    stub_writes = applied["vault_stub_writes"]
    assert len(stub_writes) == 1
    stub_path = Path(stub_writes[0]["path"])
    assert stub_path.is_relative_to(vault_root)
    assert stub_path.exists()
    content = stub_path.read_text(encoding="utf-8")
    assert "consumer_phase: phase2-stub" in content
    assert "T-Stub/1" in content
    assert "does not dispatch, send Discord, mutate Kanban" in content
