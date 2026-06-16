"""Tests for the aggregated health status endpoint."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.health_status as hs
from hermes_cli.health_status import register_health_status_routes


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_health_status_routes(app)
    return TestClient(app)


def _module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _create_sqlite_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("SELECT 1")
    conn.close()
    return path


def _install_probe_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    gateway_alive: bool = True,
    gateway_body: dict[str, Any] | None = None,
    gateway_exc: Exception | None = None,
    gateway_pid: int | None = None,
    autoresearch_status: dict[str, Any] | None = None,
    autoresearch_exc: Exception | None = None,
    kanban_path: Path | None = None,
    heartbeat_payload: dict[str, Any] | None = None,
) -> None:
    def gateway_probe() -> tuple[bool, dict[str, Any] | None]:
        if gateway_exc is not None:
            raise gateway_exc
        return gateway_alive, gateway_body

    def read_runner_status() -> dict[str, Any]:
        if autoresearch_exc is not None:
            raise autoresearch_exc
        return autoresearch_status or {
            "state": "idle",
            "heartbeat_fresh": False,
            "heartbeat_age_s": None,
        }

    if kanban_path is None:
        kanban_path = _create_sqlite_db(tmp_path / "kanban.db")
    heartbeat_path = tmp_path / "state" / "kanban_dispatcher_heartbeat.json"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(
        json.dumps(
            heartbeat_payload
            or {"last_tick_at": int(time.time()), "tick_health": "ok"}
        ),
        encoding="utf-8",
    )

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.web_server",
        _module(
            "hermes_cli.web_server",
            _probe_gateway_health=gateway_probe,
            get_running_pid=lambda: gateway_pid,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.autoresearch_view",
        _module("hermes_cli.autoresearch_view", read_runner_status=read_runner_status),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.kanban_db",
        _module(
            "hermes_cli.kanban_db",
            kanban_db_path=lambda: kanban_path,
            kanban_dispatcher_heartbeat_path=lambda: heartbeat_path,
        ),
    )


def test_all_subsystems_healthy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_probe_sources(monkeypatch, tmp_path)

    response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    assert data["schema"] == "hermes-health-v1"
    assert data["overall"] == "healthy"
    assert isinstance(data["checked_at"], int)
    assert set(data["subsystems"]) == {
        "gateway",
        "autoresearch",
        "kanban_db",
        "kanban_dispatcher",
    }
    assert {s["status"] for s in data["subsystems"].values()} == {"healthy"}


def test_gateway_running_pid_is_healthy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Same-host liveness: a running gateway PID is authoritative even when the
    # cross-container HTTP probe would report offline (no GATEWAY_HEALTH_URL).
    _install_probe_sources(
        monkeypatch,
        tmp_path,
        gateway_alive=False,
        gateway_body={"error": "gateway down"},
        gateway_pid=1258,
    )

    response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "healthy"
    assert data["subsystems"]["gateway"]["status"] == "healthy"
    assert data["subsystems"]["gateway"]["detail"] == "gateway running"


def test_one_subsystem_degraded(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_probe_sources(
        monkeypatch,
        tmp_path,
        autoresearch_status={
            "state": "running",
            "heartbeat_fresh": False,
            "heartbeat_age_s": 45.0,
        },
    )

    response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "degraded"
    assert data["subsystems"]["autoresearch"]["status"] == "degraded"
    assert data["subsystems"]["autoresearch"]["detail"] == "running"


def test_one_subsystem_offline(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_probe_sources(
        monkeypatch,
        tmp_path,
        gateway_alive=False,
        gateway_body={"error": "gateway down"},
    )

    response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "offline"
    assert data["subsystems"]["gateway"]["status"] == "offline"
    assert data["subsystems"]["gateway"]["error"] == "gateway down"


def test_probe_exception_becomes_offline(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_probe_sources(
        monkeypatch,
        tmp_path,
        gateway_exc=RuntimeError("gateway probe exploded"),
    )

    response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    gateway = data["subsystems"]["gateway"]
    assert data["overall"] == "offline"
    assert gateway["status"] == "offline"
    assert gateway["error"] == "gateway probe exploded"
    assert isinstance(gateway["latency_ms"], int)


def test_autoresearch_crashed_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_probe_sources(
        monkeypatch,
        tmp_path,
        autoresearch_status={
            "state": "crashed",
            "heartbeat_fresh": False,
            "heartbeat_age_s": 120.0,
        },
    )

    response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    autoresearch = data["subsystems"]["autoresearch"]
    assert data["overall"] == "offline"
    assert autoresearch["status"] == "offline"
    assert autoresearch["detail"] == "crashed"


def test_kanban_db_missing_file_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-kanban.db"
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.kanban_db",
        _module("hermes_cli.kanban_db", kanban_db_path=lambda: missing_path),
    )

    result = asyncio.run(hs._probe_kanban_db_status())

    assert result["status"] == "offline"
    assert result["detail"] == "database file missing"
    assert "not found:" in result["error"]


def test_kanban_db_valid_sqlite_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = _create_sqlite_db(tmp_path / "kanban.db")
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.kanban_db",
        _module("hermes_cli.kanban_db", kanban_db_path=lambda: db_path),
    )

    result = asyncio.run(hs._probe_kanban_db_status())

    assert result["status"] == "healthy"
    assert result["error"] is None


def test_kanban_dispatcher_missing_heartbeat_degraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "state" / "missing-heartbeat.json"
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.kanban_db",
        _module(
            "hermes_cli.kanban_db",
            kanban_dispatcher_heartbeat_path=lambda: missing,
        ),
    )

    result = asyncio.run(hs._probe_kanban_dispatcher_status())

    assert result["status"] == "degraded"
    assert result["detail"] == "heartbeat missing"
    assert result["heartbeat_age_s"] is None


def test_kanban_dispatcher_unreadable_heartbeat_degraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "state" / "kanban_dispatcher_heartbeat.json"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text("{not-json", encoding="utf-8")
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.kanban_db",
        _module(
            "hermes_cli.kanban_db",
            kanban_dispatcher_heartbeat_path=lambda: heartbeat,
        ),
    )

    result = asyncio.run(hs._probe_kanban_dispatcher_status())

    assert result["status"] == "degraded"
    assert result["detail"] == "heartbeat unreadable"
    assert result["heartbeat_age_s"] is None
    assert result["error"]


def test_kanban_dispatcher_stale_heartbeat_degraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "state" / "kanban_dispatcher_heartbeat.json"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text(
        json.dumps({"last_tick_at": int(time.time()) - 999, "tick_health": "ok"}),
        encoding="utf-8",
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.kanban_db",
        _module(
            "hermes_cli.kanban_db",
            kanban_dispatcher_heartbeat_path=lambda: heartbeat,
        ),
    )

    result = asyncio.run(hs._probe_kanban_dispatcher_status())

    assert result["status"] == "degraded"
    assert result["detail"] == "ok"
    assert result["error"] == "heartbeat stale"
    assert result["heartbeat_age_s"] >= 999
