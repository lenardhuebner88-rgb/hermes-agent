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
from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCredentialsError,
    RefreshExpiredError,
    Session,
)
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


class _HealthSmokePasswordProvider(DashboardAuthProvider):
    name = "health-smoke"
    display_name = "Health Smoke"
    supports_password = True

    def start_login(self, *, redirect_uri: str) -> Any:
        raise NotImplementedError

    def complete_login(self, **_kwargs: Any) -> Session:
        raise NotImplementedError

    def complete_password_login(self, *, username: str, password: str) -> Session:
        if username != "admin" or password != "hunter2":
            raise InvalidCredentialsError("bad credentials")
        return self._session(self._access_token())

    def verify_session(self, *, access_token: str) -> Session | None:
        if not access_token.startswith("health-smoke:"):
            return None
        try:
            expires_at = int(access_token.rsplit(":", 1)[1])
        except ValueError:
            return None
        if expires_at <= int(time.time()):
            return None
        return self._session(access_token, expires_at=expires_at)

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise RefreshExpiredError("not used by this smoke test")

    def revoke_session(self, *, refresh_token: str) -> None:
        return None

    def _access_token(self) -> str:
        return f"health-smoke:{int(time.time()) + 3600}"

    def _session(self, access_token: str, *, expires_at: int | None = None) -> Session:
        return Session(
            user_id="health-smoke-admin",
            email="",
            display_name="health-smoke-admin",
            org_id="",
            provider=self.name,
            expires_at=expires_at or int(time.time()) + 3600,
            access_token=access_token,
            refresh_token="health-smoke-refresh",
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


def test_probe_timeout_degrades_only_slow_subsystem(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def healthy() -> dict[str, Any]:
        return {"status": "healthy", "detail": "ok", "error": None}

    async def slow_autoresearch() -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"status": "healthy", "detail": "idle", "error": None}

    monkeypatch.setattr(hs, "PROBE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(hs, "_probe_gateway_status", healthy)
    monkeypatch.setattr(hs, "_probe_autoresearch_status", slow_autoresearch)
    monkeypatch.setattr(hs, "_probe_kanban_db_status", healthy)
    monkeypatch.setattr(hs, "_probe_kanban_dispatcher_status", healthy)

    response = client.get("/api/health-status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "degraded"
    assert data["subsystems"]["autoresearch"] == {
        "status": "degraded",
        "detail": "probe timeout after 0.0s",
        "heartbeat_age_s": None,
        "error": "timeout",
    }
    assert {
        name: subsystem["status"]
        for name, subsystem in data["subsystems"].items()
        if name != "autoresearch"
    } == {"gateway": "healthy", "kanban_db": "healthy", "kanban_dispatcher": "healthy"}


def test_health_endpoint_is_bounded_by_per_probe_timeout(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def healthy() -> dict[str, Any]:
        return {"status": "healthy", "detail": "ok", "error": None}

    async def hung_gateway() -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"status": "healthy", "detail": "gateway running", "error": None}

    monkeypatch.setattr(hs, "_probe_gateway_status", hung_gateway)
    monkeypatch.setattr(hs, "_probe_autoresearch_status", healthy)
    monkeypatch.setattr(hs, "_probe_kanban_db_status", healthy)
    monkeypatch.setattr(hs, "_probe_kanban_dispatcher_status", healthy)

    started = time.perf_counter()
    response = client.get("/api/health-status")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 5.0
    gateway = response.json()["subsystems"]["gateway"]
    assert gateway == {
        "status": "degraded",
        "detail": "probe timeout after 3.0s",
        "latency_ms": 3000,
        "error": "timeout",
    }


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


@pytest.mark.xdist_group("dashboard_auth_app_state")
def test_health_status_requires_authenticated_dashboard_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit

    async def gateway_status() -> dict[str, Any]:
        return {"status": "healthy", "detail": "gateway running", "error": None}

    async def autoresearch_status() -> dict[str, Any]:
        return {
            "status": "healthy",
            "detail": "idle",
            "heartbeat_age_s": None,
            "error": None,
        }

    async def kanban_db_status() -> dict[str, Any]:
        return {"status": "healthy", "detail": "database healthy", "error": None}

    async def kanban_dispatcher_status() -> dict[str, Any]:
        return {
            "status": "healthy",
            "detail": "ok",
            "heartbeat_age_s": 1.0,
            "error": None,
        }

    monkeypatch.setattr(hs, "_probe_gateway_status", gateway_status)
    monkeypatch.setattr(hs, "_probe_autoresearch_status", autoresearch_status)
    monkeypatch.setattr(hs, "_probe_kanban_db_status", kanban_db_status)
    monkeypatch.setattr(
        hs, "_probe_kanban_dispatcher_status", kanban_dispatcher_status
    )

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    clear_providers()
    register_provider(_HealthSmokePasswordProvider())
    _reset_password_rate_limit()
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True

    try:
        client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")

        unauthenticated = client.get("/api/health-status")
        assert unauthenticated.status_code == 401

        login = client.post(
            "/auth/password-login",
            json={
                "provider": "health-smoke",
                "username": "admin",
                "password": "hunter2",
                "next": "/api/health-status",
            },
        )
        assert login.status_code == 200
        assert login.json()["ok"] is True

        response = client.get("/api/health-status")
        assert response.status_code == 200
        data = response.json()
        assert data["overall"] == "healthy"
        assert data["subsystems"]["kanban_dispatcher"]["heartbeat_age_s"] == 1.0
    finally:
        clear_providers()
        _reset_password_rate_limit()
        web_server.app.state.bound_host = prev_host
        web_server.app.state.bound_port = prev_port
        web_server.app.state.auth_required = prev_required
