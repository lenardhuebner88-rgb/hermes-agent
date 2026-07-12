#!/usr/bin/env python3
"""watermark_check.py — one-line capacity watermark for the nightly-audit report.

CAP-S4 (grounded on the CAP-S1 recon). The homeserver has no per-host memory cap:
an uncapped process eating >2 GiB RSS, a filling root disk, or heavy swap are the
three leading indicators of the kernel-OOM / disk-full failure class this PlanSpec
exists to prevent. This module turns those three signals into ONE Discord line that
is folded into the *existing* operator health digest — **no new service, no new
timer, no new channel** (AC-1).

Contract (AC-1):

* Below every threshold it emits **nothing** (empty stdout, :func:`build_alert_line`
  returns ``None``) — the host report is byte-for-byte unchanged, no spam.
* When one or more thresholds are breached it emits exactly **one** Markdown line
  summarising every breach, e.g.::

    ⚠️ **Kapazitäts-Watermark** — Swap 62% (>50%) · Disk 91% (>88%) · 2 Prozesse
    >2G RSS außerhalb Cap (pid 1234 chrome 3.1G, pid 5678 node 2.4G)

Thresholds are **strictly greater-than** (a value exactly at the threshold is NOT an
alert — the same boundary discipline as ``browser_reap.py``): Swap > 50 %, Disk > 88 %,
process RSS > 2 GiB *and* that process is not bounded by a cgroup memory limit
("außerhalb Cap" — a capped process is left to its own cgroup OOM, it cannot take the
host down).

The pure core (:func:`evaluate`) takes an injected :class:`Metrics` snapshot so it is
fully unit-testable without real processes; :func:`collect_metrics` is the thin
psutil/cgroup-backed real collector. ``memory.max`` classification is a separate pure
helper (:func:`memmax_is_capped`) so the cgroup-v2 semantics are tested directly.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

GIB = 1024 ** 3

# Defaults — the PlanSpec thresholds (CAP-S4). All comparisons are strictly ">".
DEFAULT_SWAP_PCT = 50.0
DEFAULT_DISK_PCT = 88.0
DEFAULT_RSS_GIB = 2.0
DEFAULT_DISK_PATH = "/"
MAX_PROCS_LISTED = 3  # keep the single line inside the Discord budget


@dataclass(frozen=True)
class BigProc:
    """A process whose RSS is at/over the scan threshold, plus its cap status."""

    pid: int
    name: str
    rss_bytes: int
    capped: bool  # True == bounded by a finite cgroup memory.max (safe, own-cgroup OOM)


@dataclass(frozen=True)
class Metrics:
    """An injectable snapshot of the three watermark signals."""

    swap_used_pct: Optional[float]  # None == no swap configured (never alerts)
    disk_used_pct: float
    procs: tuple[BigProc, ...]  # candidates with rss_bytes >= scan threshold


def memmax_is_capped(memory_max_value: str) -> bool:
    """cgroup-v2 ``memory.max`` semantics: a finite number == capped, ``"max"`` == not.

    Anything unparseable is treated as *not* capped (report it) — for an OOM-avoidance
    alert it is safer to surface an ambiguous big process than to hide it.
    """
    val = memory_max_value.strip()
    if val == "max":
        return False
    try:
        int(val)
    except ValueError:
        return False
    return True


def evaluate(
    m: Metrics,
    *,
    swap_pct_threshold: float = DEFAULT_SWAP_PCT,
    disk_pct_threshold: float = DEFAULT_DISK_PCT,
    rss_threshold_bytes: int = int(DEFAULT_RSS_GIB * GIB),
) -> Optional[str]:
    """Pure watermark evaluation → the single alert line, or ``None`` if all clear.

    Strictly greater-than on every threshold, so a value exactly at the line is not an
    alert. Only *uncapped* processes over the RSS threshold count ("außerhalb Cap").
    """
    breaches: list[str] = []

    if m.swap_used_pct is not None and m.swap_used_pct > swap_pct_threshold:
        breaches.append(f"Swap {m.swap_used_pct:.0f}% (>{swap_pct_threshold:.0f}%)")

    if m.disk_used_pct > disk_pct_threshold:
        breaches.append(f"Disk {m.disk_used_pct:.0f}% (>{disk_pct_threshold:.0f}%)")

    over = sorted(
        (p for p in m.procs if not p.capped and p.rss_bytes > rss_threshold_bytes),
        key=lambda p: p.rss_bytes,
        reverse=True,
    )
    if over:
        listed = over[:MAX_PROCS_LISTED]
        detail = ", ".join(
            f"pid {p.pid} {p.name} {p.rss_bytes / GIB:.1f}G" for p in listed
        )
        if len(over) > MAX_PROCS_LISTED:
            detail += f", +{len(over) - MAX_PROCS_LISTED} weitere"
        noun = "Prozess" if len(over) == 1 else "Prozesse"
        limit_g = rss_threshold_bytes / GIB
        limit_str = f"{limit_g:.0f}" if limit_g == int(limit_g) else f"{limit_g:.1f}"
        breaches.append(f"{len(over)} {noun} >{limit_str}G RSS außerhalb Cap ({detail})")

    if not breaches:
        return None
    return "⚠️ **Kapazitäts-Watermark** — " + " · ".join(breaches)


# --- Real (psutil / cgroup-v2) collection --------------------------------------

def _proc_capped(pid: int) -> bool:
    """True when *pid*'s cgroup-v2 slice has a finite ``memory.max`` limit.

    Reads the unified (``0::``) cgroup path from ``/proc/<pid>/cgroup`` and the matching
    ``/sys/fs/cgroup/<path>/memory.max``. Any read failure → not capped (report it).
    """
    try:
        cgroup_path: Optional[str] = None
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split(":", 2)
                if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
                    cgroup_path = parts[2]
                    break
        if not cgroup_path:
            return False
        memmax = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/") / "memory.max"
        return memmax_is_capped(memmax.read_text())
    except OSError:
        return False


def collect_metrics(
    *,
    disk_path: str = DEFAULT_DISK_PATH,
    rss_threshold_bytes: int = int(DEFAULT_RSS_GIB * GIB),
) -> Metrics:
    """Snapshot the three live signals via psutil + cgroup-v2 (dependency-injected core)."""
    import psutil  # local import keeps the pure core dependency-free/testable

    swap = psutil.swap_memory()
    swap_pct: Optional[float] = None if swap.total == 0 else float(swap.percent)

    disk_pct = float(psutil.disk_usage(disk_path).percent)

    procs: list[BigProc] = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = p.info
            mem = info.get("memory_info")
            if mem is None:
                continue
            rss = int(mem.rss)
            if rss <= rss_threshold_bytes:
                continue  # only carry candidates over the scan threshold
            pid = int(info["pid"])
            procs.append(
                BigProc(
                    pid=pid,
                    name=(info.get("name") or "?")[:24],
                    rss_bytes=rss,
                    capped=_proc_capped(pid),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError):
            continue

    return Metrics(swap_used_pct=swap_pct, disk_used_pct=disk_pct, procs=tuple(procs))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument("--swap-pct", type=float, default=DEFAULT_SWAP_PCT,
                        help=f"swap-used %% alert threshold, strict > (default {DEFAULT_SWAP_PCT:.0f})")
    parser.add_argument("--disk-pct", type=float, default=DEFAULT_DISK_PCT,
                        help=f"disk-used %% alert threshold, strict > (default {DEFAULT_DISK_PCT:.0f})")
    parser.add_argument("--rss-gib", type=float, default=DEFAULT_RSS_GIB,
                        help=f"per-process RSS alert threshold in GiB, strict > (default {DEFAULT_RSS_GIB:.0f})")
    parser.add_argument("--disk-path", default=DEFAULT_DISK_PATH,
                        help=f"filesystem to check (default {DEFAULT_DISK_PATH!r})")
    args = parser.parse_args(argv)

    rss_threshold_bytes = int(args.rss_gib * GIB)
    metrics = collect_metrics(disk_path=args.disk_path, rss_threshold_bytes=rss_threshold_bytes)
    line = evaluate(
        metrics,
        swap_pct_threshold=args.swap_pct,
        disk_pct_threshold=args.disk_pct,
        rss_threshold_bytes=rss_threshold_bytes,
    )
    # Below thresholds → emit nothing so the host report stays unchanged (AC-1).
    if line:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
