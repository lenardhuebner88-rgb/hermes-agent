"""Tests for the capacity watermark check (CAP-S4).

Exercises the pure evaluation core (:func:`evaluate`) with injected :class:`Metrics`
snapshots plus the cgroup-v2 ``memory.max`` classifier (:func:`memmax_is_capped`) —
no real processes, disks, or /sys reads.

Acceptance coverage (AC-1):
* Below every threshold → ``None`` (the host report stays unchanged, no spam).
* At least one breach → exactly one Markdown line naming each breach.
* Strict greater-than boundary: a value exactly at the threshold does not alert.
* Only *uncapped* (außerhalb Cap) big processes count; capped ones are ignored.
"""

from __future__ import annotations

import pytest

from scripts import watermark_check as wc

GIB = wc.GIB


def _proc(pid, name, rss_gib, *, capped):
    return wc.BigProc(pid=pid, name=name, rss_bytes=int(rss_gib * GIB), capped=capped)


def _metrics(*, swap=0.0, disk=0.0, procs=()):
    return wc.Metrics(swap_used_pct=swap, disk_used_pct=disk, procs=tuple(procs))


# --- all-clear: report unchanged, no spam (AC-1) -------------------------------

def test_all_below_thresholds_returns_none():
    assert wc.evaluate(_metrics(swap=10.0, disk=40.0)) is None


def test_no_swap_configured_never_alerts():
    m = wc.Metrics(swap_used_pct=None, disk_used_pct=10.0, procs=())
    assert wc.evaluate(m) is None


# --- strict greater-than boundary ----------------------------------------------

def test_swap_exactly_at_threshold_is_not_an_alert():
    assert wc.evaluate(_metrics(swap=50.0)) is None


def test_disk_exactly_at_threshold_is_not_an_alert():
    assert wc.evaluate(_metrics(disk=88.0)) is None


def test_rss_exactly_at_threshold_is_not_an_alert():
    m = _metrics(procs=[_proc(1, "chrome", 2.0, capped=False)])  # exactly 2 GiB
    assert wc.evaluate(m) is None


# --- individual breaches -------------------------------------------------------

def test_swap_over_threshold_alerts():
    line = wc.evaluate(_metrics(swap=62.0))
    assert line is not None
    assert line.startswith("⚠️ **Kapazitäts-Watermark**")
    assert "Swap 62% (>50%)" in line


def test_disk_over_threshold_alerts():
    line = wc.evaluate(_metrics(disk=91.4))
    assert "Disk 91% (>88%)" in line


def test_uncapped_big_process_alerts_with_pid_name_size():
    m = _metrics(procs=[_proc(1234, "chrome", 3.1, capped=False)])
    line = wc.evaluate(m)
    assert line is not None
    assert "1 Prozess >2G RSS außerhalb Cap" in line
    assert "pid 1234 chrome 3.1G" in line


# --- "außerhalb Cap": capped processes are ignored -----------------------------

def test_capped_big_process_is_ignored():
    m = _metrics(procs=[_proc(1234, "hermes", 4.0, capped=True)])
    assert wc.evaluate(m) is None


def test_only_uncapped_counted_when_mixed():
    m = _metrics(procs=[
        _proc(1, "capped", 5.0, capped=True),
        _proc(2, "loose", 2.5, capped=False),
    ])
    line = wc.evaluate(m)
    assert "1 Prozess" in line
    assert "pid 2 loose 2.5G" in line
    assert "pid 1" not in line


def test_multiple_big_procs_sorted_and_capped_at_three():
    procs = [
        _proc(1, "a", 2.2, capped=False),
        _proc(2, "b", 5.0, capped=False),
        _proc(3, "c", 3.0, capped=False),
        _proc(4, "d", 4.0, capped=False),
    ]
    line = wc.evaluate(_metrics(procs=procs))
    assert "4 Prozesse >2G RSS außerhalb Cap" in line
    # top-3 by RSS listed (b=5, d=4, c=3), a=2.2 folds into "+1 weitere"
    assert "pid 2 b 5.0G" in line
    assert "pid 4 d 4.0G" in line
    assert "pid 3 c 3.0G" in line
    assert "+1 weitere" in line
    assert "pid 1 a" not in line


# --- all three at once → single line -------------------------------------------

def test_all_three_breaches_join_on_one_line():
    m = _metrics(swap=62.0, disk=91.0, procs=[_proc(9, "node", 2.4, capped=False)])
    line = wc.evaluate(m)
    assert line.count("\n") == 0
    assert " · " in line
    assert "Swap 62% (>50%)" in line
    assert "Disk 91% (>88%)" in line
    assert "pid 9 node 2.4G" in line


# --- custom thresholds honoured ------------------------------------------------

def test_custom_thresholds_are_respected():
    m = _metrics(swap=55.0)
    assert wc.evaluate(m, swap_pct_threshold=60.0) is None
    assert wc.evaluate(m, swap_pct_threshold=50.0) is not None


# --- cgroup-v2 memory.max classifier -------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("max\n", False),          # unified "no limit"
    ("max", False),
    ("2147483648\n", True),    # a finite byte limit == capped
    ("0", True),
    ("", False),               # unreadable → treat as uncapped (report)
    ("garbage", False),
])
def test_memmax_is_capped(value, expected):
    assert wc.memmax_is_capped(value) is expected


# ---------------------------------------------------------------------------
# Collector + contract pinning
# ---------------------------------------------------------------------------

def test_dataclasses_are_frozen():
    """The snapshot records must be immutable — an alert pipeline that
    could mutate its own metrics would silently re-classify a breach."""
    proc = wc.BigProc(pid=1, name="x", rss_bytes=10, capped=False)
    metrics = wc.Metrics(swap_used_pct=None, disk_used_pct=0.0, procs=(proc,))
    with pytest.raises(AttributeError):
        proc.capped = True
    with pytest.raises(AttributeError):
        metrics.disk_used_pct = 99.0


def test_proc_capped_fails_closed_on_unreadable_cgroup():
    """A pid whose cgroup file cannot be read must classify as NOT capped
    (fail closed: report the process) — never as safely capped."""
    assert wc._proc_capped(2**40) is False  # beyond any pid_max — cannot exist


def test_collect_metrics_carries_only_procs_strictly_over_threshold(monkeypatch):
    """The scan threshold is strict: a process at EXACTLY the threshold is
    not carried, one byte over is — the alert boundary must not wobble."""
    import psutil
    from collections import namedtuple

    Mem = namedtuple("Mem", ["rss"])
    Swap = namedtuple("Swap", ["total", "percent"])
    Disk = namedtuple("Disk", ["percent"])

    class _P:
        def __init__(self, info):
            self.info = info

    exact = int(wc.DEFAULT_RSS_GIB * wc.GIB)
    procs = [
        _P({"pid": 1, "name": "exact", "memory_info": Mem(exact)}),
        _P({"pid": 2, "name": "over", "memory_info": Mem(exact + 1)}),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(procs))
    monkeypatch.setattr(psutil, "swap_memory", lambda: Swap(total=0, percent=0.0))
    monkeypatch.setattr(psutil, "disk_usage", lambda path: Disk(percent=10.0))
    monkeypatch.setattr(wc, "_proc_capped", lambda pid: True)

    metrics = wc.collect_metrics(rss_threshold_bytes=exact)

    assert metrics.swap_used_pct is None
    assert metrics.disk_used_pct == 10.0
    assert [(p.pid, p.rss_bytes) for p in metrics.procs] == [(2, exact + 1)]
