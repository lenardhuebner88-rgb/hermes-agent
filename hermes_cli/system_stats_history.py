"""In-process CPU/RAM sample history for the Jarvis vitals sparklines.

Slice B1 (Jarvis-Produktreife): a daemon sampler records
``{ts, cpu_percent, mem_percent}`` every 15 s into a bounded ring (2 h). The
dashboard endpoint ``GET /api/system/stats/history`` serves that ring so the
Jarvis-Vitals (Frontend-Slice G4) get real history. Purely additive — the
existing ``/api/system/stats`` handler in ``web_server.py`` stays untouched.

psutil-degraded: a missing psutil (or a failed read) never raises into the
request path. The sampler keeps running with ``None`` values and the endpoint
reports a short German note in ``errors[]``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Optional

from fastapi import FastAPI

logger = logging.getLogger(__name__)

# 15 s cadence × 480 slots = 7200 s = 2 h ring window.
SAMPLE_INTERVAL_S = 15
RING_MAXLEN = 480
DEFAULT_MINUTES = 120

_sampler_lock = threading.Lock()
_sampler_thread: Optional[threading.Thread] = None
_sampler_stop = threading.Event()

# The ring + last degraded-read note. Written by the sampler thread, read by
# request handlers. Single deque.append / attribute read are GIL-atomic, and a
# torn read of the advisory error string is harmless.
_samples: "deque[dict[str, Any]]" = deque(maxlen=RING_MAXLEN)
_last_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Sampling (psutil-degraded)
# ---------------------------------------------------------------------------


def _read_psutil() -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return ``(cpu_percent, mem_percent, error)``; degrade to None, never raise."""
    try:
        import psutil  # type: ignore
    except Exception:
        return None, None, "psutil nicht verfügbar – CPU- und RAM-Werte sind None."

    cpu: Optional[float] = None
    mem: Optional[float] = None
    problems: list[str] = []
    try:
        # interval=None → non-blocking; successive 15 s calls yield accurate %.
        cpu = psutil.cpu_percent(interval=None)
    except Exception:
        problems.append("CPU-Auslastung")
    try:
        mem = psutil.virtual_memory().percent
    except Exception:
        problems.append("RAM-Auslastung")

    if problems:
        return cpu, mem, "psutil-Fehler: " + " und ".join(problems) + " nicht lesbar."
    return cpu, mem, None


def _sample_once(now: Optional[float] = None) -> dict[str, Any]:
    """Take one sample, append it to the ring. Never raises."""
    global _last_error
    cpu, mem, err = _read_psutil()
    _last_error = err
    sample = {
        "ts": time.time() if now is None else now,
        "cpu_percent": cpu,
        "mem_percent": mem,
    }
    _samples.append(sample)
    return sample


def _loop(interval: float) -> None:
    while not _sampler_stop.is_set():
        try:
            _sample_once()
        except Exception:  # pragma: no cover - defensive, _sample_once is safe
            logger.warning("system_stats_history sample failed", exc_info=True)
        if _sampler_stop.wait(interval):
            break


def start_sampler(interval_s: float = SAMPLE_INTERVAL_S) -> bool:
    """Start the daemon sampler. Idempotent; kill-switch via env.

    Returns True if a new thread was started, False if skipped/already running.
    Does nothing when ``HERMES_SYSTEM_STATS_HISTORY=0``.

    The app lifespan also runs inside ``with TestClient(app)`` blocks across the
    test suite; a sampler started there would spin a real thread per test app.
    Under pytest it therefore only starts when forced with
    ``HERMES_SYSTEM_STATS_HISTORY=1`` (mirrors ``agent_questions.start_poller``).
    """
    global _sampler_thread

    env = os.environ.get("HERMES_SYSTEM_STATS_HISTORY")
    if env == "0":
        logger.info("system_stats_history sampler disabled (HERMES_SYSTEM_STATS_HISTORY=0)")
        return False
    if env != "1" and "pytest" in sys.modules:
        return False

    with _sampler_lock:
        if _sampler_thread is not None and _sampler_thread.is_alive():
            return False
        _sampler_stop.clear()
        interval = max(0.01, float(interval_s))
        thread = threading.Thread(
            target=_loop,
            args=(interval,),
            name="system-stats-history-sampler",
            daemon=True,
        )
        _sampler_thread = thread
        thread.start()
        logger.info("system_stats_history sampler started (interval_s=%s)", interval)
        return True


def stop_sampler() -> None:
    """Stop the daemon sampler (tests / shutdown). Safe if never started."""
    global _sampler_thread
    _sampler_stop.set()
    with _sampler_lock:
        thread = _sampler_thread
        _sampler_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _coerce_minutes(minutes: Any) -> int:
    max_minutes = (RING_MAXLEN * SAMPLE_INTERVAL_S) // 60  # 120
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_MINUTES
    return max(1, min(value, max_minutes))


def _coerce_step(step: Any) -> int:
    try:
        value = int(step)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _mean(values: list[Optional[float]]) -> Optional[float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 1)


def _bucket(samples: list[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    """Bucket-mean every ``step`` consecutive samples (ts = bucket start)."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(samples), step):
        chunk = samples[i : i + step]
        out.append(
            {
                "ts": chunk[0]["ts"],
                "cpu_percent": _mean([c["cpu_percent"] for c in chunk]),
                "mem_percent": _mean([c["mem_percent"] for c in chunk]),
            }
        )
    return out


def _build_response(minutes: Any, step: Any) -> dict[str, Any]:
    """Shape the ring into the G4 contract; safe on empty/degraded state."""
    minutes = _coerce_minutes(minutes)
    step = _coerce_step(step)
    errors = [_last_error] if _last_error else []

    snapshot = list(_samples)
    if not snapshot:
        return {"interval_s": SAMPLE_INTERVAL_S, "window_s": 0, "samples": [], "errors": errors}

    # Window is relative to the newest sample (deterministic, idle-safe).
    cutoff = snapshot[-1]["ts"] - minutes * 60
    window = [s for s in snapshot if s["ts"] >= cutoff]
    buckets = _bucket(window, step)

    window_s = int(buckets[-1]["ts"] - buckets[0]["ts"]) if buckets else 0
    return {
        "interval_s": SAMPLE_INTERVAL_S,
        "window_s": window_s,
        "samples": buckets,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


def register_system_stats_history(app: FastAPI) -> None:
    """Mount ``GET /api/system/stats/history`` (additive to /api/system/stats)."""

    @app.get("/api/system/stats/history")
    async def get_system_stats_history(minutes: int = DEFAULT_MINUTES, step: int = 1):
        return _build_response(minutes, step)
