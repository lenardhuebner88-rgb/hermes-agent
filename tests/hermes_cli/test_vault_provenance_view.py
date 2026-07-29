"""Tests for the /api/vault/provenance TTL cache (2026-06-11 perf audit C2).

The collector forks a Python subprocess that scans the Vault (up to 8s);
the tile polls every 20s from every open client. The TTL cache must keep
that to at most one subprocess per window, and errors must not be cached.
"""
from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import vault_provenance_view as vpv


def _fresh_client(monkeypatch):
    importlib.reload(vpv)
    app = FastAPI()
    vpv.register_vault_provenance_routes(app)
    return TestClient(app)


def test_polls_within_ttl_hit_cache(monkeypatch):
    client = _fresh_client(monkeypatch)
    calls = {"n": 0}

    def fake_collect():
        calls["n"] += 1
        return {
            "schema": vpv._SCHEMA,
            "error": None,
            "stale_count": 0,
            "open_sessions": [],
            "recent_receipts": [],
        }

    monkeypatch.setattr(vpv, "_collect_sync", fake_collect)

    for _ in range(4):
        r = client.get("/api/vault/provenance")
        assert r.status_code == 200
        assert r.json()["error"] is None
    assert calls["n"] == 1, "subsequent polls within the TTL must reuse the cache"


def test_error_results_are_not_cached(monkeypatch):
    client = _fresh_client(monkeypatch)
    calls = {"n": 0}

    def flaky_collect():
        calls["n"] += 1
        if calls["n"] == 1:
            return vpv._empty("boom")
        return {
            "schema": vpv._SCHEMA,
            "error": None,
            "stale_count": 0,
            "open_sessions": [],
            "recent_receipts": [],
        }

    monkeypatch.setattr(vpv, "_collect_sync", flaky_collect)

    first = client.get("/api/vault/provenance").json()
    assert first["error"] == "boom"
    second = client.get("/api/vault/provenance").json()
    assert second["error"] is None, "an error payload must not be served from cache"
    assert calls["n"] == 2


def test_expired_ttl_refreshes(monkeypatch):
    client = _fresh_client(monkeypatch)
    calls = {"n": 0}

    def fake_collect():
        calls["n"] += 1
        return {
            "schema": vpv._SCHEMA,
            "error": None,
            "stale_count": calls["n"],
            "open_sessions": [],
            "recent_receipts": [],
        }

    monkeypatch.setattr(vpv, "_collect_sync", fake_collect)
    monkeypatch.setattr(vpv, "_CACHE_TTL_S", 0.0)

    assert client.get("/api/vault/provenance").json()["stale_count"] == 1
    assert client.get("/api/vault/provenance").json()["stale_count"] == 2
    assert calls["n"] == 2


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_empty_helper_stale_count_is_zero():
    """Kill const_offset L45 (0 -> 1): _empty() must report stale_count 0."""
    result = vpv._empty("some error")
    assert result["stale_count"] == 0
    assert result["error"] == "some error"
    assert result["open_sessions"] == []
    assert result["recent_receipts"] == []


def test_collect_sync_stale_count_counts_exactly(monkeypatch):
    """Kill const_offset L77 (1 -> 2): exactly one stale session must
    produce stale_count == 1, not 2."""
    importlib.reload(vpv)

    def fake_run(*args, **kwargs):
        class P:
            returncode = 0
            stdout = '{"open_sessions": [{"stale": true}, {"stale": false}], "recent_receipts": []}'
            stderr = ""
        return P()

    monkeypatch.setattr(vpv.subprocess, "run", fake_run)
    result = vpv._collect_sync()
    assert result["stale_count"] == 1
    assert result["error"] is None
