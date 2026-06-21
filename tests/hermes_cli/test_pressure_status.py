"""Tests for the read-only dashboard pressure endpoint."""
from __future__ import annotations

import inspect
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.pressure_status as ps
from hermes_cli.pressure_status import register_pressure_status_routes


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_pressure_status_routes(app)
    return TestClient(app)


def _base_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checked_at": int(time.time()),
        "host": {
            "cpu_percent": 24.0,
            "load_avg": [2.1, 2.8, 3.4],
            "cpu_count": 12,
            "memory_percent": 52.0,
        },
        "dashboard": {
            "pid": 1243,
            "rss_mb": 716.5,
            "cpu_percent": 8.5,
            "cpu_weight": 300,
            "cpu_quota": "infinity",
            "tasks_current": 23,
        },
        "pressure_sources": [],
        "access": {
            "tailnet": "direct",
            "api_latency_ms": 190.0,
            "detail": "tailnet direct",
        },
        "token_pressure": {"class": "unknown", "pct": None},
        "errors": [],
    }
    payload.update(overrides)
    return payload


def test_unthrottled_test_processes_mark_dashboard_busy_without_leaking_cmdlines() -> None:
    body = ps.build_pressure_status(
        _base_payload(
            pressure_sources=[
                {
                    "kind": "test",
                    "label": "pytest",
                    "count": 4,
                    "cpu_percent": 370.0,
                    "rss_mb": 410.0,
                    "scope": "session scope",
                    "scope_kind": "session",
                    "throttled": False,
                }
            ]
        )
    )

    assert body["schema"] == "hermes-pressure-v1"
    assert body["overall"] == "busy"
    assert body["cause"] == "Ungedrosselte Testprozesse in Session-Scope"
    assert body["pressure_sources"][0]["label"] == "pytest"

    serialized = json.dumps(body).lower()
    assert "/home/" not in serialized
    assert ".worktrees/" not in serialized
    assert "run_tests_parallel.py" not in serialized
    assert "bearer" not in serialized
    assert "token=" not in serialized


def test_saturated_when_host_and_api_cross_thresholds() -> None:
    body = ps.build_pressure_status(
        _base_payload(
            host={
                "cpu_percent": 94.0,
                "load_avg": [14.0, 13.2, 10.0],
                "cpu_count": 12,
                "memory_percent": 84.0,
            },
            access={"tailnet": "direct", "api_latency_ms": 1800.0, "detail": "tailnet direct"},
        )
    )

    assert body["overall"] == "saturated"
    assert body["cause"] == "Host und API sind unter deutlichem Druck"


def test_recommends_tailnet_relay_as_next_read_only_lever() -> None:
    body = ps.build_pressure_status(
        _base_payload(
            access={"tailnet": "relay", "api_latency_ms": 820.0, "detail": "tailnet relay path active"},
            pressure_sources=[
                {
                    "kind": "test",
                    "label": "pytest",
                    "count": 2,
                    "cpu_percent": 180.0,
                    "rss_mb": 300.0,
                    "scope": "session scope",
                    "scope_kind": "session",
                    "throttled": False,
                }
            ],
        )
    )

    assert body["recommendation"] == {
        "label": "Tailnet relay",
        "detail": "Tailnet nutzt Relay; Direktpfad pruefen.",
        "tone": "amber",
    }


def test_recommends_running_tests_as_next_read_only_lever() -> None:
    body = ps.build_pressure_status(
        _base_payload(
            pressure_sources=[
                {
                    "kind": "test",
                    "label": "pytest",
                    "count": 1,
                    "cpu_percent": 120.0,
                    "rss_mb": 300.0,
                    "scope": "session scope",
                    "scope_kind": "session",
                    "throttled": False,
                },
                {
                    "kind": "browser_test",
                    "label": "browser test",
                    "count": 1,
                    "cpu_percent": 90.0,
                    "rss_mb": 500.0,
                    "scope": "session scope",
                    "scope_kind": "session",
                    "throttled": False,
                },
            ],
        )
    )

    assert body["recommendation"] == {
        "label": "Tests laufen",
        "detail": "2 Test-/Browser-Prozesse aktiv.",
        "tone": "amber",
    }


def test_unthrottled_browser_tests_mark_dashboard_busy() -> None:
    body = ps.build_pressure_status(
        _base_payload(
            pressure_sources=[
                {
                    "kind": "browser_test",
                    "label": "browser test",
                    "count": 1,
                    "cpu_percent": 85.0,
                    "rss_mb": 500.0,
                    "scope": "session scope",
                    "scope_kind": "session",
                    "throttled": False,
                }
            ]
        )
    )

    assert body["overall"] == "busy"
    assert body["cause"] == "Ungedrosselte Testprozesse in Session-Scope"
    assert body["recommendation"]["label"] == "Tests laufen"


def test_pressure_source_sort_keeps_browser_tests_with_tests() -> None:
    sources = [
        {"kind": "hermes_service", "cpu_percent": 5.0, "count": 1},
        {"kind": "agent", "cpu_percent": 30.0, "count": 1},
        {"kind": "browser_test", "cpu_percent": 10.0, "count": 1},
        {"kind": "test", "cpu_percent": 1.0, "count": 1},
    ]

    ordered = sorted(sources, key=ps._pressure_source_sort_key)

    assert [source["kind"] for source in ordered] == [
        "test",
        "browser_test",
        "agent",
        "hermes_service",
    ]


@pytest.mark.parametrize(
    ("name", "cmdline", "expected"),
    [
        ("pytest", ["pytest", "tests/hermes_cli/test_pressure_status.py"], ("test", "pytest")),
        ("chromium", ["chromium", "--type=renderer", "ms-playwright"], ("browser_test", "browser test")),
        ("codex", ["codex", "exec"], ("agent", "codex")),
        ("python3", ["python3", "-m", "hermes_cli.main", "dashboard"], ("hermes_service", "dashboard")),
    ],
)
def test_classifies_known_pressure_roles_without_returning_raw_cmdlines(
    name: str,
    cmdline: list[str],
    expected: tuple[str, str],
) -> None:
    assert ps._classify_process(name, cmdline) == expected


def test_cpu_delta_sampler_reports_process_spikes_after_first_sample() -> None:
    cache: dict[int, tuple[float, float]] = {}

    assert ps._cpu_percent_from_sample(cache, 42, 100.0, 5.0) == 0.0
    assert ps._cpu_percent_from_sample(cache, 42, 102.0, 9.0) == 200.0
    assert ps._cpu_percent_from_sample(cache, 42, 103.0, 9.0) == 0.0


def test_route_handler_is_sync_to_avoid_event_loop_pressure() -> None:
    app = FastAPI()
    register_pressure_status_routes(app)

    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/pressure-status")

    assert not inspect.iscoroutinefunction(route.endpoint)


def test_route_degrades_to_error_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("raw path /home/piet/.env token=secret")

    monkeypatch.setattr(ps, "snapshot", boom)

    response = client.get("/api/pressure-status")

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "hermes-pressure-v1"
    assert body["overall"] == "unknown"
    assert body["errors"]
    assert "/home/" not in json.dumps(body)
    assert "token=secret" not in json.dumps(body)


def test_snapshot_uses_short_top_level_cache(monkeypatch):
    from hermes_cli import pressure_status

    calls = {"host": 0}

    monkeypatch.setattr(pressure_status, "_PRESSURE_CACHE", None)
    monkeypatch.setattr(pressure_status, "_collect_dashboard", lambda errors: {"rss_mb": 1})
    monkeypatch.setattr(pressure_status, "_collect_pressure_sources", lambda errors: [])
    monkeypatch.setattr(pressure_status, "_collect_access", lambda errors: {"tailnet": "direct", "api_latency_ms": 10})
    monkeypatch.setattr(pressure_status, "_collect_token_pressure", lambda: {"class": "unknown", "pct": None})

    def collect_host(errors):
        calls["host"] += 1
        return {"cpu_count": 4, "cpu_percent": 1, "load_avg": [0.1, 0.1, 0.1], "memory_percent": 20}

    monkeypatch.setattr(pressure_status, "_collect_host", collect_host)

    ticks = iter([100.0, 105.001])
    monkeypatch.setattr(pressure_status.time, "monotonic", lambda: next(ticks))

    first = pressure_status.snapshot(force=True)
    second = pressure_status.snapshot()

    assert first == second
    assert calls["host"] == 1
