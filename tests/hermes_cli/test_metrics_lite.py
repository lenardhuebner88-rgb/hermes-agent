"""Tests for the in-process self-metrics ring + endpoint + middleware."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.metrics_lite as ml
from hermes_cli.metrics_lite import register_metrics_lite_routes


@pytest.fixture(autouse=True)
def _clean_rings() -> None:
    ml.reset()
    yield
    ml.reset()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_metrics_lite_routes(app)
    return TestClient(app)


def test_empty_ring_returns_empty_groups(client: TestClient) -> None:
    res = client.get("/api/metrics-lite")
    assert res.status_code == 200
    body = res.json()
    assert body["schema"] == "hermes-metrics-lite-v1"
    assert body["groups"] == {}
    assert "uptime_seconds" in body


def test_record_counts_and_error_rate() -> None:
    for _ in range(8):
        ml.record("/api/x", 10.0, False)
    for _ in range(2):
        ml.record("/api/x", 50.0, True)
    snap = ml.snapshot()["groups"]["/api/x"]
    assert snap["count"] == 10
    assert snap["error_count"] == 2
    assert snap["error_rate"] == 0.2


def test_percentiles() -> None:
    for i in range(1, 101):
        ml.record("/api/p", float(i), False)
    snap = ml.snapshot()["groups"]["/api/p"]
    assert 49.0 <= snap["p50_ms"] <= 51.0
    assert 94.0 <= snap["p95_ms"] <= 96.0


def test_record_tolerates_garbage_args() -> None:
    ml.record("/api/g", "not-a-number", None)  # type: ignore[arg-type]
    # Bad latency is dropped silently; group may exist but must not crash snapshot.
    ml.snapshot()


def test_ring_is_bounded() -> None:
    for i in range(ml._RING_SIZE + 100):
        ml.record("/api/b", float(i), False)
    assert len(ml._RINGS["/api/b"]) == ml._RING_SIZE
    assert ml.snapshot()["groups"]["/api/b"]["count"] == ml._RING_SIZE


def test_route_group_collapses_ids() -> None:
    assert ml.route_group("/api/plugins/kanban/runs/123/inspect") == "/api/plugins/kanban/runs/*/inspect"
    assert ml.route_group("/api/cron/observability/output/16dd6ac01fc0") == "/api/cron/observability/output/*"
    # Word-only paths (no digits) are NOT collapsed.
    assert ml.route_group("/api/health-status") == "/api/health-status"
    assert ml.route_group("/api/cron/observability") == "/api/cron/observability"
    assert ml.route_group("/assets/index-abc.js") == "static"
    assert ml.route_group("/control/crons") == "spa"


def test_snapshot_exception_becomes_error_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict:
        raise RuntimeError("snapshot blew up")

    monkeypatch.setattr(ml, "snapshot", boom)
    res = client.get("/api/metrics-lite")
    assert res.status_code == 200
    body = res.json()
    assert body["groups"] == {}
    assert "error" in body


def test_middleware_records_and_propagates_exceptions() -> None:
    ml.reset()
    app = FastAPI()

    @app.middleware("http")
    async def metrics_mw(request, call_next):
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            ml.record(ml.route_group(request.url.path), (time.perf_counter() - start) * 1000.0, status_code >= 500)

    @app.get("/api/ok")
    async def ok() -> dict:
        return {"ok": True}

    @app.get("/api/boom")
    async def boom() -> dict:
        raise RuntimeError("route exploded")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/api/ok")
    client.get("/api/boom")

    groups = ml.snapshot()["groups"]
    assert groups["/api/ok"]["count"] == 1
    assert groups["/api/ok"]["error_count"] == 0
    # A route exception is recorded as an error (status defaulted to 500) and
    # still propagates (TestClient turns it into a 500 with raise_server_exceptions=False).
    assert groups["/api/boom"]["count"] == 1
    assert groups["/api/boom"]["error_count"] == 1
