#!/usr/bin/env python3
"""browser_reap.py — reap orphaned + over-aged Playwright-MCP / headless-Chrome procs.

Browser-automation surfaces (Claude/Codex Playwright MCP, ``control_shot.py``) leak
long-lived headless Chromium/node processes when the spawning worker dies mid-run —
the same failure class as the vite orphan that ``ui-preview.sh reap`` already sweeps
(a preview ran orphaned for 15 days once). This is the browser half of that reaper.

Design (CAP-S2, grounded on the CAP-S1 recon):

* A process is reaped ONLY when THREE necessary conditions all hold:
    1. its cmdline matches one of the NARROW positive signatures below
       (package/output-dir tokens — never a bare ``chrome`` or ``--headless``,
       which are shared by the Meet bot and interactive browsers),
    2. its age exceeds the threshold (default 6h — no legitimate MCP/control-shot
       automation session runs that long), AND
    3. it looks ORPHANED (parent is init/subreaper or already dead). This guards a
       live, long-running interactive Claude Playwright MCP whose parent process is
       still alive — that is NOT reaped even past the age threshold.
* Meet-bot browser/parent processes are HARD-EXCLUDED by their fake-media / module
  markers even if a positive token also appears.
* Default is DRY-RUN: it prints ``WOULD-KILL`` journal lines and signals nothing.
  Pass ``--apply`` to actually SIGTERM (then SIGKILL survivors after a grace wait).

Called by ``ui-preview.sh``'s reap sweep (systemd ``ui-preview-reap.timer``, every
15 min). The pure core (:func:`plan_reap`) takes an injected process table plus
injected kill/liveness callables so it is fully unit-testable without real processes.
"""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

# --- Narrow POSITIVE signatures ------------------------------------------------
# A process must match one of these to even be a candidate. Package / output-dir /
# browser-cache tokens are stable and specific; a bare "chrome"/"--headless" match
# would be over-broad (Meet + interactive browsers share it). Refs: CAP-S1 recon
# — .claude.json:1616-1633, scripts/control_shot.py:84-101.
PLAYWRIGHT_MCP_PKG = "@playwright/mcp@"
PLAYWRIGHT_MCP_OUTPUT = "/home/piet/.hermes/playwright-mcp-output"
MS_PLAYWRIGHT_CACHE = "ms-playwright"  # chromium executable / user-data under Playwright's browser cache

# --- Hard EXCLUSION markers ----------------------------------------------------
# Never reap a process carrying one of these, even if a positive token matches.
# Google Meet bot: fake-media flags (meet_bot.py:514-548) + module path
# (process_manager.py:159-170). Meet detaches with start_new_session, so excluding
# by marker — not by process group — is the safe way to avoid orphaning its tree.
EXCLUDE_MARKERS: tuple[str, ...] = (
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "plugins.google_meet.meet_bot",
    "google_meet",
)

# Parents that mean "orphaned" — init / a systemd user-manager subreaper.
SUBREAPER_MARKERS: tuple[str, ...] = (
    "systemd --user",
    "/lib/systemd/systemd --user",
    "/usr/lib/systemd/systemd --user",
)

DEFAULT_THRESHOLD_HOURS = 6.0
GRACE_SECONDS = 3  # SIGTERM → wait → SIGKILL survivors (mirrors orchestration-reaper)


@dataclass(frozen=True)
class ProcInfo:
    """A minimal, injectable process record (no psutil dependency in the core)."""

    pid: int
    ppid: int
    create_time: float  # epoch seconds (psutil create_time semantics)
    cmdline: tuple[str, ...]

    @property
    def cmd_str(self) -> str:
        return " ".join(self.cmdline)


@dataclass
class ReapCandidate:
    pid: int
    age_seconds: float
    signature: str
    cmd: str
    signal_sent: str = "DRY-RUN"  # DRY-RUN | SIGTERM | SIGKILL


def classify_signature(cmd_str: str) -> Optional[str]:
    """Return the matched positive signature name, or None.

    Hard exclusions win over positive matches: a Meet browser that also happens to
    live under the Playwright browser cache still returns None.
    """
    for marker in EXCLUDE_MARKERS:
        if marker in cmd_str:
            return None
    if PLAYWRIGHT_MCP_PKG in cmd_str:
        return "playwright-mcp-pkg"
    if PLAYWRIGHT_MCP_OUTPUT in cmd_str:
        return "playwright-mcp-output"
    # A headless Chromium launched from Playwright's own browser cache — this is the
    # control_shot / MCP browser child. Both conditions are required so a system or
    # interactive Chrome (not under ms-playwright) never matches.
    if MS_PLAYWRIGHT_CACHE in cmd_str and "--headless" in cmd_str:
        return "playwright-headless-chromium"
    return None


def is_orphaned(proc: ProcInfo, live_pids: set[int], cmd_by_pid: dict[int, str]) -> bool:
    """True when the process has no live, meaningful parent (safe to reap).

    Orphaned == reparented to init (ppid<=1), parent no longer alive, or parent is a
    systemd user-manager subreaper. A process whose parent is a live interactive
    ``claude``/``codex``/``node`` session is NOT orphaned — that is an in-use MCP.
    """
    ppid = proc.ppid
    if ppid <= 1:
        return True
    if ppid not in live_pids:
        return True
    parent_cmd = cmd_by_pid.get(ppid, "")
    return any(m in parent_cmd for m in SUBREAPER_MARKERS)


def plan_reap(
    procs: Sequence[ProcInfo],
    *,
    now: float,
    threshold_seconds: float,
) -> list[ReapCandidate]:
    """Pure selection: which processes SHOULD be reaped, and why.

    Returns candidates only; performs no side effects. Kill criteria are the three
    necessary conditions: signature match AND age>threshold AND orphaned.
    """
    live_pids = {p.pid for p in procs}
    cmd_by_pid = {p.pid: p.cmd_str for p in procs}
    candidates: list[ReapCandidate] = []
    for p in procs:
        sig = classify_signature(p.cmd_str)
        if sig is None:
            continue
        age = now - p.create_time
        if age < threshold_seconds:
            continue  # fresh — an active session (AC-2)
        if not is_orphaned(p, live_pids, cmd_by_pid):
            continue  # live parent — an in-use MCP, leave it
        candidates.append(
            ReapCandidate(pid=p.pid, age_seconds=age, signature=sig, cmd=p.cmd_str)
        )
    return candidates


def format_journal_line(c: ReapCandidate, *, dry_run: bool) -> str:
    """One observable line per (would-be) kill: PID, age, signature (AC-3)."""
    verb = "WOULD-KILL" if dry_run else "KILL"
    cmd = c.cmd if len(c.cmd) <= 200 else c.cmd[:197] + "..."
    return (
        f"browser-reap: {verb} pid={c.pid} age={c.age_seconds / 3600:.2f}h "
        f"sig={c.signature} signal={c.signal_sent} cmd={shlex.quote(cmd)}"
    )


def execute_reap(
    candidates: Sequence[ReapCandidate],
    *,
    dry_run: bool,
    kill_fn: Callable[[int, int], None],
    is_alive_fn: Callable[[int], bool],
    log_fn: Callable[[str], None],
    sleep_fn: Callable[[float], None],
    grace_seconds: float = GRACE_SECONDS,
) -> list[ReapCandidate]:
    """SIGTERM candidates, wait, then SIGKILL survivors. Journals every action.

    In dry-run nothing is signalled — each candidate is logged as ``WOULD-KILL``.
    Returns the candidates with ``signal_sent`` recorded.
    """
    if dry_run:
        for c in candidates:
            log_fn(format_journal_line(c, dry_run=True))
        return list(candidates)

    for c in candidates:
        c.signal_sent = "SIGTERM"
        try:
            kill_fn(c.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone between scan and signal — benign
        log_fn(format_journal_line(c, dry_run=False))

    if candidates:
        sleep_fn(grace_seconds)

    # Re-check: SIGKILL only the candidates that ignored SIGTERM.
    for c in candidates:
        if is_alive_fn(c.pid):
            c.signal_sent = "SIGKILL"
            try:
                kill_fn(c.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            log_fn(format_journal_line(c, dry_run=False))
    return list(candidates)


# --- Real (psutil-backed) collection & default side-effect callables -----------

def collect_procs() -> list[ProcInfo]:
    """Snapshot the live process table via psutil (skips self + argless procs)."""
    import psutil  # local import: keeps the pure core dependency-free/testable

    me = os.getpid()
    out: list[ProcInfo] = []
    for p in psutil.process_iter(["pid", "ppid", "create_time", "cmdline"]):
        try:
            info = p.info
            pid = info["pid"]
            if pid == me:
                continue
            cmd = info.get("cmdline") or []
            if not cmd:
                continue
            # never let the reaper match itself
            if any("browser_reap" in part for part in cmd):
                continue
            out.append(
                ProcInfo(
                    pid=pid,
                    ppid=info.get("ppid") or 0,
                    create_time=info.get("create_time") or 0.0,
                    cmdline=tuple(cmd),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
            continue
    return out


def _os_kill(pid: int, sig: int) -> None:
    os.kill(pid, sig)


def _os_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument(
        "--apply",
        action="store_true",
        help="actually signal matched processes (default: dry-run, signal nothing)",
    )
    grp.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit no-op (this is the default in the first rollout)",
    )
    parser.add_argument(
        "--threshold-hours",
        type=float,
        default=DEFAULT_THRESHOLD_HOURS,
        help=f"minimum age to consider a match orphaned (default {DEFAULT_THRESHOLD_HOURS})",
    )
    parser.add_argument(
        "--now",
        type=float,
        default=None,
        help="override 'now' epoch seconds (testing/repro)",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply
    now = args.now if args.now is not None else time.time()
    threshold_seconds = args.threshold_hours * 3600.0

    procs = collect_procs()
    candidates = plan_reap(procs, now=now, threshold_seconds=threshold_seconds)

    def log(line: str) -> None:
        print(line, flush=True)

    execute_reap(
        candidates,
        dry_run=dry_run,
        kill_fn=_os_kill,
        is_alive_fn=_os_is_alive,
        log_fn=log,
        sleep_fn=time.sleep,
    )

    mode = "dry-run" if dry_run else "apply"
    print(
        f"browser-reap: done mode={mode} scanned={len(procs)} "
        f"candidates={len(candidates)} threshold_h={args.threshold_hours}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
