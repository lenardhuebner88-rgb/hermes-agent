"""Subsystem C — autonomous release + rollback (Plan→Board pipeline, 2026-07-05).

Unit tests ALWAYS mock deploy/rollback/HTTP — never touch the live service.
"""

from __future__ import annotations

import pytest

from hermes_cli import auto_release


# ---------------------------------------------------------------------------
# C2: run_live_test — depth executor
# ---------------------------------------------------------------------------


def test_smoke_depth_calls_health():
    """smoke = health/status payload check (truth = API payload, not screenshot)."""
    calls = []

    def fake_fetch(path, timeout=8.0):
        calls.append(path)
        return {"version": "0.18.0", "gateway_running": True, "config_version": 3}

    res = auto_release.run_live_test("smoke", fetch=fake_fetch)
    assert res.passed is True
    assert res.held is False
    assert "/api/status" in calls


def test_smoke_depth_fails_on_bad_payload():
    def fake_fetch(path, timeout=8.0):
        return {"not": "a status payload"}

    res = auto_release.run_live_test("smoke", fetch=fake_fetch)
    assert res.passed is False
    assert res.held is False


def test_smoke_depth_fails_on_fetch_error():
    def fake_fetch(path, timeout=8.0):
        raise OSError("connection refused")

    res = auto_release.run_live_test("smoke", fetch=fake_fetch)
    assert res.passed is False


def test_contract_depth_asserts_expected_payload():
    def fake_fetch(path, timeout=8.0):
        return {"version": "0.18.0", "gateway_running": False, "config_version": 3}

    res = auto_release.run_live_test(
        "contract",
        fetch=fake_fetch,
        contract={"path": "/api/status", "expect": {"gateway_running": True}},
    )
    assert res.passed is False
    assert "gateway_running" in res.detail


def test_contract_depth_passes_on_match():
    def fake_fetch(path, timeout=8.0):
        return {"version": "0.18.0", "gateway_running": True}

    res = auto_release.run_live_test(
        "contract",
        fetch=fake_fetch,
        contract={"path": "/api/status", "expect": {"gateway_running": True}},
    )
    assert res.passed is True


def test_ui_real_depth_always_held():
    """ui-real stays operator-gated — never autonomous."""

    def fake_fetch(path, timeout=8.0):  # pragma: no cover — must not be called
        raise AssertionError("ui-real must not probe anything")

    res = auto_release.run_live_test("ui-real", fetch=fake_fetch)
    assert res.held is True
    assert res.passed is False


def test_empty_depth_trivially_passes():
    res = auto_release.run_live_test("", fetch=lambda p, timeout=8.0: {})
    assert res.passed is True
    assert res.detail == "no live test configured"
