"""Tests for hermes_cli/system_stats_history.py (Slice B1, Jarvis-Vitals).

Covers the in-process sampler (writes samples, ring cap, psutil degradation),
the GET /api/system/stats/history contract (shape, minutes filter, step
bucket-mean, empty + error paths) against a minimal app, and the sampler
thread lifecycle (idempotent start, clean stop → no leak).
"""

import sys
import time

import pytest
from fastapi import FastAPI

from hermes_cli import system_stats_history as mod


class _FakeVM:
    percent = 42.0


class _FakePsutil:
    @staticmethod
    def cpu_percent(interval=None):
        return 12.5

    @staticmethod
    def virtual_memory():
        return _FakeVM()


@pytest.fixture(autouse=True)
def _clean_state():
    """Isolate the module ring/error/thread between tests (no leak)."""
    mod.stop_sampler()
    mod._samples.clear()
    mod._last_error = None
    yield
    mod.stop_sampler()
    mod._samples.clear()
    mod._last_error = None


def _client():
    from starlette.testclient import TestClient

    app = FastAPI()
    mod.register_system_stats_history(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------


def test_sampler_writes_samples(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil())
    mod._sample_once(now=1000.0)
    mod._sample_once(now=1015.0)
    assert len(mod._samples) == 2
    assert mod._samples[0] == {"ts": 1000.0, "cpu_percent": 12.5, "mem_percent": 42.0}
    assert mod._last_error is None


def test_ring_caps_at_480(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil())
    for i in range(500):
        mod._sample_once(now=float(i))
    assert len(mod._samples) == mod.RING_MAXLEN == 480
    # oldest 20 dropped.
    assert mod._samples[0]["ts"] == 20.0
    assert mod._samples[-1]["ts"] == 499.0


def test_sampler_degrades_without_psutil(monkeypatch):
    # None in sys.modules makes `import psutil` raise ImportError.
    monkeypatch.setitem(sys.modules, "psutil", None)
    sample = mod._sample_once(now=5.0)  # must NOT raise
    assert sample["cpu_percent"] is None
    assert sample["mem_percent"] is None
    assert mod._last_error  # German note recorded
    assert len(mod._samples) == 1


def test_sampler_thread_accumulates_and_stops(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil())
    monkeypatch.setenv("HERMES_SYSTEM_STATS_HISTORY", "1")  # force under pytest
    assert mod.start_sampler(interval_s=0.01) is True
    # Idempotent: a second start is a no-op while the thread lives.
    assert mod.start_sampler(interval_s=0.01) is False
    time.sleep(0.2)  # short interval, not real-time waiting (~20 samples)
    mod.stop_sampler()
    assert len(mod._samples) >= 4
    assert mod._sampler_thread is None  # joined → no thread leak


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


def test_endpoint_empty_state_returns_200_empty_samples():
    r = _client().get("/api/system/stats/history")
    assert r.status_code == 200
    body = r.json()
    assert body["interval_s"] == 15
    assert body["window_s"] == 0
    assert body["samples"] == []
    assert body["errors"] == []


def test_endpoint_contract_shape():
    base = 1_000_000.0
    for i in range(5):
        mod._samples.append(
            {"ts": base + i * 15, "cpu_percent": 10.0 + i, "mem_percent": 50.0 + i}
        )
    body = _client().get("/api/system/stats/history").json()
    assert body["interval_s"] == 15
    assert body["window_s"] == 60  # 4 intervals × 15 s
    assert len(body["samples"]) == 5
    assert body["samples"][0] == {"ts": base, "cpu_percent": 10.0, "mem_percent": 50.0}
    assert body["samples"][-1]["ts"] == base + 60
    assert body["errors"] == []


def test_endpoint_minutes_filter():
    base = 1_000_000.0
    # 10 samples, 15 s apart → newest = base+135.
    for i in range(10):
        mod._samples.append(
            {"ts": base + i * 15, "cpu_percent": float(i), "mem_percent": float(i)}
        )
    # minutes=1 → cutoff = newest-60 = base+75 → keep i>=5 → 5 samples.
    body = _client().get("/api/system/stats/history?minutes=1").json()
    assert len(body["samples"]) == 5
    assert body["samples"][0]["ts"] == base + 5 * 15


def test_endpoint_step_bucket_mean():
    base = 1_000_000.0
    vals = [(0, 10.0, 40.0), (15, 20.0, 60.0), (30, 30.0, 80.0), (45, 40.0, 100.0)]
    for dt, cpu, mem in vals:
        mod._samples.append({"ts": base + dt, "cpu_percent": cpu, "mem_percent": mem})
    # 4 samples / step 2 → 2 buckets; ts = bucket start.
    body = _client().get("/api/system/stats/history?step=2").json()
    assert len(body["samples"]) == 2
    # bucket 1: mean(10,20)=15 cpu, mean(40,60)=50 mem.
    assert body["samples"][0] == {"ts": base, "cpu_percent": 15.0, "mem_percent": 50.0}
    # bucket 2: mean(30,40)=35 cpu, mean(80,100)=90 mem.
    assert body["samples"][1] == {"ts": base + 30, "cpu_percent": 35.0, "mem_percent": 90.0}
    assert body["window_s"] == 30  # (base+30) - base


def test_endpoint_error_path_returns_200_with_errors(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    mod._sample_once(now=1_000_000.0)  # degrades → None values + error note
    r = _client().get("/api/system/stats/history")
    assert r.status_code == 200
    body = r.json()
    assert body["errors"]
    assert "psutil" in body["errors"][0]
    assert body["samples"][0]["cpu_percent"] is None
    assert body["samples"][0]["mem_percent"] is None
