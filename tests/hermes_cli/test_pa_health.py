from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.pa_chat as pa_chat
import hermes_cli.pa_health as health


@pytest.fixture
def isolated_health_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    vault = tmp_path / "vault"
    kanban_db = hermes_home / "kanban.db"
    receipt_dir = vault / "03-Agents" / "Codex" / "receipts"
    hermes_home.mkdir(parents=True)
    receipt_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_db))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    conn = sqlite3.connect(kanban_db)
    conn.execute(
        "CREATE TABLE task_events ("
        "id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()
    pa_chat.PAStore().ensure_schema()
    return {
        "home": home,
        "hermes_home": hermes_home,
        "vault": vault,
        "kanban_db": kanban_db,
        "receipt_dir": receipt_dir,
    }


def _add_turn(*, now: int, failed: bool = False, text: str = "Antwort") -> None:
    store = pa_chat.PAStore()
    turn_id = store.create_turn(
        text="Frage",
        engine="sol",
        model=pa_chat.SOL_MODEL,
        project_scope=None,
        attachments=[],
        now=now,
    )
    assert store.set_running(turn_id, now=now)
    if failed:
        store.fail_turn(turn_id, text, now=now)
    else:
        store.finish_turn(turn_id, text, now=now)


def _set_fresh_world(paths: dict[str, Path], *, now: int) -> None:
    conn = sqlite3.connect(paths["kanban_db"])
    conn.execute(
        "INSERT INTO task_events(task_id, kind, created_at) VALUES ('t_1','created',?)",
        (now - 60,),
    )
    conn.commit()
    conn.close()
    receipt = paths["receipt_dir"] / "ship-receipt.md"
    receipt.write_text("# Ship\n", encoding="utf-8")
    os.utime(receipt, (now - 60, now - 60))
    store = pa_chat.PAStore()
    with store.connect() as conn:
        conn.executemany(
            "INSERT INTO pa_watcher_state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            [
                ("last_tick_at", str(now - 60), now - 60),
                ("interval_seconds", "60", now - 60),
                ("enabled", "1", now - 60),
            ],
        )


def test_healthy_sources_report_real_watcher_and_unconfigured_push_truthfully(
    isolated_health_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_CLAIMS_SUB"):
        monkeypatch.delenv(name, raising=False)
    now = 2_000_000_000
    _add_turn(now=now - 30)
    _set_fresh_world(isolated_health_sources, now=now)

    payload = health.build_pa_health(now=now)

    assert payload["generated_at"] == now
    assert payload["checks"]["engine"]["status"] == "healthy"
    assert payload["checks"]["engine"]["error_rate"] == 0.0
    assert payload["checks"]["kanban_events"]["status"] == "healthy"
    assert payload["checks"]["receipts"]["status"] == "healthy"
    assert payload["checks"]["watcher"] == {
        "status": "healthy",
        "enabled": True,
        "latest_ts": now - 60,
        "age_seconds": 60,
        "interval_seconds": 60,
        "stale_after_seconds": 180,
    }
    assert payload["checks"]["push"] == {
        "status": "degraded",
        "reason": "VAPID-Konfiguration unvollständig",
        "subscriptions": 0,
    }
    assert payload["ok"] is False
    assert [item["check"] for item in payload["degraded"]] == ["push"]


def _set_vapid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("VAPID_CLAIMS_SUB", "mailto:test@example.com")


def _add_subscription() -> None:
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, keys_p256dh, keys_auth, "
            "created_at, fail_count) VALUES ('https://push.example/s1', 'k1', 'k2', 1, 0)"
        )
        conn.commit()


def test_push_degraded_without_subscription(
    isolated_health_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_vapid(monkeypatch)
    now = 2_000_000_000
    _add_turn(now=now - 30)
    _set_fresh_world(isolated_health_sources, now=now)

    payload = health.build_pa_health(now=now)

    assert payload["checks"]["push"]["status"] == "degraded"
    assert payload["checks"]["push"]["subscriptions"] == 0
    assert "Kein Gerät" in payload["checks"]["push"]["reason"]
    assert payload["ok"] is False


def test_push_healthy_with_subscription(
    isolated_health_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_vapid(monkeypatch)
    _add_subscription()
    now = 2_000_000_000
    _add_turn(now=now - 30)
    _set_fresh_world(isolated_health_sources, now=now)

    payload = health.build_pa_health(now=now)

    assert payload["checks"]["push"] == {"status": "healthy", "subscriptions": 1}
    assert payload["ok"] is True
    assert payload["degraded"] == []


def test_engine_error_rate_and_last_error_degrade(
    isolated_health_sources: dict[str, Path],
) -> None:
    now = 2_000_000_000
    for offset in range(4):
        _add_turn(now=now - 100 - offset)
    _add_turn(now=now - 10, failed=True, text="Provider timeout")
    _set_fresh_world(isolated_health_sources, now=now)

    payload = health.build_pa_health(now=now)
    engine = payload["checks"]["engine"]

    assert engine["status"] == "degraded"
    assert engine["sample_size"] == 5
    assert engine["error_count"] == 1
    assert engine["error_rate"] == health.ENGINE_ERROR_RATE_THRESHOLD
    assert engine["last_error"] == {"text": "Provider timeout", "ts": now - 10}
    assert payload["degraded"][0] == {
        "check": "engine",
        "reason": "Engine-Fehlerrate 20% erreicht den Schwellwert 20%",
        "since_ts": now - 10,
    }


@pytest.mark.parametrize(
    ("check", "threshold", "collector_name"),
    [
        ("kanban_events", health.KANBAN_STALE_AFTER_SECONDS, "kanban"),
        ("receipts", health.RECEIPT_STALE_AFTER_SECONDS, "receipt"),
    ],
)
def test_blind_threshold_is_strictly_greater_than_boundary(
    isolated_health_sources: dict[str, Path],
    check: str,
    threshold: int,
    collector_name: str,
) -> None:
    now = 2_000_000_000
    _add_turn(now=now - 10)
    _set_fresh_world(isolated_health_sources, now=now)
    if collector_name == "kanban":
        conn = sqlite3.connect(isolated_health_sources["kanban_db"])
        conn.execute("UPDATE task_events SET created_at=?", (now - threshold,))
        conn.commit()
        conn.close()
    else:
        receipt = isolated_health_sources["receipt_dir"] / "ship-receipt.md"
        os.utime(receipt, (now - threshold, now - threshold))

    boundary = health.build_pa_health(now=now)
    assert boundary["checks"][check]["status"] == "healthy"

    if collector_name == "kanban":
        conn = sqlite3.connect(isolated_health_sources["kanban_db"])
        conn.execute("UPDATE task_events SET created_at=created_at-1")
        conn.commit()
        conn.close()
    else:
        receipt = isolated_health_sources["receipt_dir"] / "ship-receipt.md"
        os.utime(receipt, (now - threshold - 1, now - threshold - 1))

    stale = health.build_pa_health(now=now)
    assert stale["checks"][check]["status"] == "degraded"
    assert stale["checks"][check]["age_seconds"] == threshold + 1
    assert check in {item["check"] for item in stale["degraded"]}


def test_source_failure_is_isolated(
    isolated_health_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    _set_fresh_world(isolated_health_sources, now=now)
    monkeypatch.setattr(
        health,
        "_collect_engine_health",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is busy")),
    )

    payload = health.build_pa_health(now=now)

    assert payload["checks"]["engine"]["status"] == "degraded"
    assert payload["checks"]["engine"]["source_error"] == "database is busy"
    assert payload["checks"]["kanban_events"]["status"] == "healthy"
    assert payload["checks"]["receipts"]["status"] == "healthy"


def test_watcher_health_flips_only_after_three_intervals(
    isolated_health_sources: dict[str, Path],
) -> None:
    now = 2_000_000_000
    _set_fresh_world(isolated_health_sources, now=now)
    store = pa_chat.PAStore()
    with store.connect() as conn:
        conn.execute(
            "UPDATE pa_watcher_state SET value=? WHERE key='last_tick_at'",
            (str(now - 180),),
        )

    boundary = health.build_pa_health(now=now)
    assert boundary["checks"]["watcher"]["status"] == "healthy"
    assert boundary["checks"]["watcher"]["age_seconds"] == 180

    with store.connect() as conn:
        conn.execute(
            "UPDATE pa_watcher_state SET value=? WHERE key='last_tick_at'",
            (str(now - 181),),
        )
    stale = health.build_pa_health(now=now)
    assert stale["checks"]["watcher"]["status"] == "degraded"
    assert stale["checks"]["watcher"]["stale_after_seconds"] == 180
    assert "watcher" in {item["check"] for item in stale["degraded"]}


def test_endpoint_returns_200_instead_of_500_on_catastrophic_failure(
    isolated_health_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health,
        "build_pa_health",
        lambda: (_ for _ in ()).throw(RuntimeError("collector exploded")),
    )
    app = FastAPI()
    pa_chat.register_pa_routes(app)

    with TestClient(app) as client:
        response = client.get("/api/pa/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["degraded"][0]["check"] == "self_check"
    assert "collector exploded" in payload["degraded"][0]["reason"]
