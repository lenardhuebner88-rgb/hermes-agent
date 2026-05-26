"""P3-1.B contract pin: hub_watcher._memory_growth_alert.

Validates Agent-B's "Memory Saturation Loop" mitigation (sprint 2026-05-17):
when MEMORY.md grows by more than ``threshold_bytes`` over ``window_hours``
AND no successful trim happened in the window, the helper emits a durable
signal at ``state/daily_digest_signals.jsonl`` for the next Daily-Digest run.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest


HUB_WATCHER = Path("/home/piet/.hermes/bin/hub_watcher.py")


def _load_mod(tmp_path, monkeypatch):
    if not HUB_WATCHER.exists():
        pytest.skip(f"local ops script not present: {HUB_WATCHER}")
    spec = importlib.util.spec_from_file_location(
        "hub_watcher_under_test", str(HUB_WATCHER)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Redirect log + signal paths into tmp dir so the test doesn't write live state.
    monkeypatch.setattr(mod, "LOG_FILE", tmp_path / "hub-watcher.jsonl")
    monkeypatch.setattr(mod, "DAILY_DIGEST_SIGNALS", tmp_path / "daily_digest_signals.jsonl")
    return mod


def _write_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _t(hours_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat(
        timespec="seconds"
    )


def test_growth_above_threshold_without_trim_emits_signal(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    # Three runs in the last 24h, each adding ~700B, no successful trim.
    _write_log(
        mod.LOG_FILE,
        [
            {"ts": _t(20), "type": "auto-curate", "action": "no-op", "pre_bytes": 1000, "post_bytes": 1700},
            {"ts": _t(10), "type": "auto-curate", "action": "no-op", "pre_bytes": 1700, "post_bytes": 2400},
            {"ts": _t(2),  "type": "auto-curate", "action": "no-op", "pre_bytes": 2400, "post_bytes": 3000},
        ],
    )
    result = mod._memory_growth_alert(window_hours=24, threshold_bytes=1500)
    assert result["action"] == "alert_emitted"
    assert result["net_growth_bytes"] == 2000
    assert result["trim_count"] == 0
    assert mod.DAILY_DIGEST_SIGNALS.exists()
    signals = [json.loads(line) for line in mod.DAILY_DIGEST_SIGNALS.read_text().splitlines() if line.strip()]
    assert len(signals) == 1
    assert signals[0]["kind"] == "hub-memory-growth-alert"
    assert signals[0]["evidence"]["net_growth_bytes"] == 2000


def test_growth_below_threshold_silent(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    _write_log(
        mod.LOG_FILE,
        [
            {"ts": _t(20), "type": "auto-curate", "action": "no-op", "pre_bytes": 1000, "post_bytes": 1200},
            {"ts": _t(2),  "type": "auto-curate", "action": "no-op", "pre_bytes": 1200, "post_bytes": 1400},
        ],
    )
    result = mod._memory_growth_alert(window_hours=24, threshold_bytes=1500)
    assert result["action"] == "noop"
    assert result["reason"] == "below_threshold_or_trim_observed"
    assert result["net_growth_bytes"] == 400
    assert not mod.DAILY_DIGEST_SIGNALS.exists()


def test_trim_in_window_silences_alert_even_if_growth_above_threshold(tmp_path, monkeypatch):
    """If a trim succeeded inside the window, suppress the alert — auto-curate
    is still functional and Piet doesn't need a fresh ping."""
    mod = _load_mod(tmp_path, monkeypatch)
    _write_log(
        mod.LOG_FILE,
        [
            {"ts": _t(20), "type": "auto-curate", "action": "no-op", "pre_bytes": 1000, "post_bytes": 2000},
            {"ts": _t(10), "type": "auto-curate", "action": "trimmed", "pre_bytes": 2200, "post_bytes": 1800},
            {"ts": _t(2),  "type": "auto-curate", "action": "no-op", "pre_bytes": 1800, "post_bytes": 3500},
        ],
    )
    result = mod._memory_growth_alert(window_hours=24, threshold_bytes=1500)
    # Growth high, but trim_count=1 → silent.
    assert result["action"] == "noop"
    assert result["trim_count"] == 1


def test_log_missing_is_noop(tmp_path, monkeypatch):
    mod = _load_mod(tmp_path, monkeypatch)
    # LOG_FILE intentionally not created.
    assert not mod.LOG_FILE.exists()
    result = mod._memory_growth_alert()
    assert result["action"] == "noop"
    assert result["reason"] == "log_missing"
