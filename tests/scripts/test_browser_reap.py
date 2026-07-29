"""Tests for the browser reaper (CAP-S2).

Exercises the pure selection core (:func:`plan_reap`) and the SIGTERM→SIGKILL
executor (:func:`execute_reap`) with an injected fake process table plus recorded
kill/liveness callables — no real processes are spawned or signalled.

Acceptance coverage:
* AC-1: only signature-matched AND over-aged (AND orphaned) processes are killed;
        dry-run signals nothing.
* AC-2: a fresh (<6h) MCP process is preserved; a non-MCP / Meet-bot Chrome is
        preserved.
* AC-3: one journal line per kill carrying PID, age, and signature.
"""

from __future__ import annotations

import signal

import pytest

from scripts import browser_reap as br

HOUR = 3600.0
NOW = 1_000_000.0  # fixed epoch "now" so ages are deterministic
THRESHOLD = br.DEFAULT_THRESHOLD_HOURS * HOUR  # 6h


def _proc(pid, cmdline, *, age_hours=0.0, ppid=1):
    return br.ProcInfo(
        pid=pid,
        ppid=ppid,
        create_time=NOW - age_hours * HOUR,
        cmdline=tuple(cmdline),
    )


# --- signature classification --------------------------------------------------

def test_classify_playwright_mcp_package():
    cmd = "npx -y @playwright/mcp@0.0.76 --headless --no-sandbox --isolated"
    assert br.classify_signature(cmd) == "playwright-mcp-pkg"


def test_classify_playwright_mcp_output_dir():
    cmd = "node server --output-dir /home/piet/.hermes/playwright-mcp-output"
    assert br.classify_signature(cmd) == "playwright-mcp-output"


def test_classify_headless_chromium_under_playwright_cache():
    cmd = "/home/piet/.cache/ms-playwright/chromium-1181/chrome-linux/chrome --headless=new --remote-debugging-pipe"
    assert br.classify_signature(cmd) == "playwright-headless-chromium"


def test_classify_ignores_generic_headless_chrome():
    # headless but NOT under the Playwright cache -> not our concern
    assert br.classify_signature("/usr/bin/google-chrome --headless --dump-dom") is None


def test_classify_ignores_non_headless_chrome():
    assert br.classify_signature("/usr/bin/google-chrome --profile-directory=Default") is None


def test_classify_meet_bot_excluded_even_with_playwright_cache():
    # A Meet browser can be headless AND under ms-playwright, but the fake-media
    # marker must veto it.
    cmd = (
        "/home/piet/.cache/ms-playwright/chromium-1181/chrome-linux/chrome --headless "
        "--use-fake-ui-for-media-stream --use-fake-device-for-media-stream"
    )
    assert br.classify_signature(cmd) is None


def test_classify_meet_bot_parent_module_excluded():
    assert br.classify_signature("python -m plugins.google_meet.meet_bot") is None


# --- orphan predicate ----------------------------------------------------------

def test_orphaned_when_reparented_to_init():
    p = _proc(10, ["chrome"], ppid=1)
    assert br.is_orphaned(p, live_pids={10}, cmd_by_pid={10: "chrome"}) is True


def test_orphaned_when_parent_dead():
    p = _proc(10, ["chrome"], ppid=999)
    assert br.is_orphaned(p, live_pids={10}, cmd_by_pid={10: "chrome"}) is True


def test_orphaned_when_parent_is_systemd_user_subreaper():
    p = _proc(10, ["chrome"], ppid=2)
    assert br.is_orphaned(p, live_pids={10, 2}, cmd_by_pid={2: "/lib/systemd/systemd --user"}) is True


def test_not_orphaned_with_live_interactive_parent():
    p = _proc(10, ["chrome"], ppid=2)
    cmd_by_pid = {2: "node /home/piet/.claude/... claude", 10: "chrome"}
    assert br.is_orphaned(p, live_pids={10, 2}, cmd_by_pid=cmd_by_pid) is False


# --- plan_reap selection (AC-1, AC-2) ------------------------------------------

def test_over_aged_orphaned_mcp_is_selected():
    procs = [
        _proc(100, ["npx", "-y", "@playwright/mcp@0.0.76", "--headless"], age_hours=7.0, ppid=1),
    ]
    picks = br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD)
    assert [c.pid for c in picks] == [100]
    assert picks[0].signature == "playwright-mcp-pkg"
    assert picks[0].age_seconds == pytest.approx(7.0 * HOUR)


def test_fresh_mcp_process_is_not_selected():
    # AC-2: <6h MCP process survives
    procs = [
        _proc(101, ["npx", "-y", "@playwright/mcp@0.0.76", "--headless"], age_hours=1.0, ppid=1),
    ]
    assert br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD) == []


def test_mcp_at_exactly_threshold_is_not_selected():
    # AC-1 boundary: age == threshold (exactly 6h) must NOT be reaped — the AC
    # requires strictly "Alter > Schwelle". Guards the `<` vs `<=` regression.
    procs = [
        _proc(104, ["npx", "-y", "@playwright/mcp@0.0.76", "--headless"], age_hours=6.0, ppid=1),
    ]
    assert br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD) == []


def test_mcp_just_over_threshold_is_selected():
    # AC-1 boundary companion: an infinitesimally over-6h match IS reaped, so the
    # `<=` fix does not accidentally exclude everything above the threshold.
    procs = [
        _proc(105, ["npx", "-y", "@playwright/mcp@0.0.76", "--headless"], ppid=1),
    ]
    # age = threshold + 1s
    procs = [br.ProcInfo(pid=105, ppid=1, create_time=NOW - (THRESHOLD + 1.0), cmdline=procs[0].cmdline)]
    picks = br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD)
    assert [c.pid for c in picks] == [105]


def test_non_mcp_chrome_is_not_selected():
    # AC-2: a non-headless / non-Playwright Chrome, even if ancient + orphaned
    procs = [
        _proc(102, ["/usr/bin/google-chrome", "--profile-directory=Default"], age_hours=48.0, ppid=1),
    ]
    assert br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD) == []


def test_meet_bot_browser_is_not_selected():
    # AC-2: Meet bot browser (fake-media), old + orphaned, still preserved
    procs = [
        _proc(
            103,
            [
                "/home/piet/.cache/ms-playwright/chromium-1181/chrome-linux/chrome",
                "--headless",
                "--use-fake-ui-for-media-stream",
            ],
            age_hours=9.0,
            ppid=1,
        ),
    ]
    assert br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD) == []


def test_live_long_running_mcp_is_not_selected():
    # A matched, ancient MCP whose parent is a LIVE interactive claude session is
    # NOT orphaned -> preserved (protects an in-use interactive MCP).
    procs = [
        _proc(50, ["node", "claude"], age_hours=20.0, ppid=1),  # the live claude session
        _proc(200, ["npx", "-y", "@playwright/mcp@0.0.76", "--headless"], age_hours=8.0, ppid=50),
    ]
    assert br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD) == []


def test_mixed_table_selects_only_the_leaked_browser():
    procs = [
        _proc(50, ["node", "claude"], age_hours=20.0, ppid=1),
        _proc(201, ["npx", "@playwright/mcp@0.0.76"], age_hours=1.0, ppid=50),  # fresh, live parent
        _proc(
            202,
            ["/home/piet/.cache/ms-playwright/chromium-1181/chrome-linux/chrome", "--headless"],
            age_hours=10.0,
            ppid=1,  # orphaned control_shot leak
        ),
        _proc(203, ["/usr/bin/google-chrome"], age_hours=99.0, ppid=1),  # unrelated
    ]
    picks = br.plan_reap(procs, now=NOW, threshold_seconds=THRESHOLD)
    assert [c.pid for c in picks] == [202]
    assert picks[0].signature == "playwright-headless-chromium"


# --- execute_reap (AC-1 dry-run, AC-3 journal, SIGTERM/SIGKILL) ----------------

def _recorder():
    kills: list[tuple[int, int]] = []
    logs: list[str] = []
    sleeps: list[float] = []
    return kills, logs, sleeps


def test_dry_run_signals_nothing_but_journals(capsys):
    # AC-1: dry-run is default and must not signal.
    c = br.ReapCandidate(pid=300, age_seconds=7 * HOUR, signature="playwright-mcp-pkg", cmd="npx @playwright/mcp@0.0.76")
    kills, logs, sleeps = _recorder()
    out = br.execute_reap(
        [c],
        dry_run=True,
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        is_alive_fn=lambda pid: True,
        log_fn=logs.append,
        sleep_fn=sleeps.append,
    )
    assert kills == []  # nothing signalled
    assert out[0].signal_sent == "DRY-RUN"
    # AC-3: journal line carries PID, age, signature
    assert len(logs) == 1
    line = logs[0]
    assert line.startswith("browser-reap: WOULD-KILL")
    assert "pid=300" in line
    assert "age=7.00h" in line
    assert "sig=playwright-mcp-pkg" in line


def test_apply_sigterm_then_no_sigkill_when_process_exits():
    c = br.ReapCandidate(pid=301, age_seconds=8 * HOUR, signature="playwright-mcp-pkg", cmd="x")
    kills, logs, sleeps = _recorder()
    br.execute_reap(
        [c],
        dry_run=False,
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        is_alive_fn=lambda pid: False,  # died after SIGTERM
        log_fn=logs.append,
        sleep_fn=sleeps.append,
    )
    assert kills == [(301, signal.SIGTERM)]  # no SIGKILL
    assert sleeps == [br.GRACE_SECONDS]
    assert c.signal_sent == "SIGTERM"
    assert any("KILL pid=301" in ln and "SIGTERM" in ln for ln in logs)


def test_apply_escalates_to_sigkill_for_survivor():
    c = br.ReapCandidate(pid=302, age_seconds=8 * HOUR, signature="playwright-headless-chromium", cmd="chrome")
    kills, logs, sleeps = _recorder()
    br.execute_reap(
        [c],
        dry_run=False,
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        is_alive_fn=lambda pid: True,  # ignored SIGTERM
        log_fn=logs.append,
        sleep_fn=sleeps.append,
    )
    assert kills == [(302, signal.SIGTERM), (302, signal.SIGKILL)]
    assert c.signal_sent == "SIGKILL"


def test_execute_reap_tolerates_already_gone_process():
    c = br.ReapCandidate(pid=303, age_seconds=8 * HOUR, signature="playwright-mcp-pkg", cmd="x")

    def boom(pid, sig):
        raise ProcessLookupError

    # Should not raise even though the process vanished between scan and signal.
    br.execute_reap(
        [c],
        dry_run=False,
        kill_fn=boom,
        is_alive_fn=lambda pid: False,
        log_fn=lambda s: None,
        sleep_fn=lambda s: None,
    )


def test_no_candidates_means_no_sleep():
    kills, logs, sleeps = _recorder()
    br.execute_reap(
        [],
        dry_run=False,
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        is_alive_fn=lambda pid: True,
        log_fn=logs.append,
        sleep_fn=sleeps.append,
    )
    assert kills == []
    assert sleeps == []


# --- CLI wiring ----------------------------------------------------------------

def test_main_defaults_to_dry_run(monkeypatch, capsys):
    leaked = br.ProcInfo(
        pid=400,
        ppid=1,
        create_time=NOW - 10 * HOUR,
        cmdline=("npx", "-y", "@playwright/mcp@0.0.76", "--headless"),
    )
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(br, "collect_procs", lambda: [leaked])
    monkeypatch.setattr(br, "_os_kill", lambda pid, sig: signalled.append((pid, sig)))

    rc = br.main(["--now", str(NOW)])
    assert rc == 0
    assert signalled == []  # default dry-run signals nothing
    out = capsys.readouterr()
    assert "WOULD-KILL pid=400" in out.out
    assert "mode=dry-run" in out.err


def test_main_apply_signals(monkeypatch, capsys):
    leaked = br.ProcInfo(
        pid=401,
        ppid=1,
        create_time=NOW - 10 * HOUR,
        cmdline=("npx", "-y", "@playwright/mcp@0.0.76", "--headless"),
    )
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(br, "collect_procs", lambda: [leaked])
    monkeypatch.setattr(br, "_os_kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(br, "_os_is_alive", lambda pid: False)

    rc = br.main(["--apply", "--now", str(NOW)])
    assert rc == 0
    assert signalled == [(401, signal.SIGTERM)]
    out = capsys.readouterr()
    assert "mode=apply" in out.err


# ---------------------------------------------------------------------------
# Edge pinning
# ---------------------------------------------------------------------------

def test_procinfo_is_frozen():
    """The injected process record must be immutable — a reaper that can
    mutate its own scan input could silently reclassify a process."""
    p = _proc(1, ["x"])
    with pytest.raises(AttributeError):
        p.pid = 99


def test_orphaned_ppid_exactly_one_is_orphaned_even_with_live_init():
    """ppid==1 is the orphan boundary itself — it must count as orphaned
    even when pid 1 is alive and not a subreaper (a plain init)."""
    p = _proc(50, ["chrome"], ppid=1)
    assert br.is_orphaned(p, live_pids={1, 60}, cmd_by_pid={1: "init"}) is True


def test_apply_sigterm_journal_line_is_kill_not_would_kill():
    """The real-run SIGTERM journal line must read KILL, never WOULD-KILL —
    an operator grepping the journal for WOULD-KILL must only see dry-runs."""
    c = br.ReapCandidate(pid=310, age_seconds=8 * HOUR, signature="playwright-mcp-pkg", cmd="x")
    kills, logs, sleeps = _recorder()
    br.execute_reap(
        [c],
        dry_run=False,
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        is_alive_fn=lambda pid: False,
        log_fn=logs.append,
        sleep_fn=sleeps.append,
    )
    assert len(logs) == 1
    assert logs[0].startswith("browser-reap: KILL pid=310")


def test_apply_sigkill_journal_line_is_kill_not_would_kill():
    """Same for the SIGKILL re-check line of a SIGTERM-resistant process."""
    c = br.ReapCandidate(pid=311, age_seconds=8 * HOUR, signature="playwright-mcp-pkg", cmd="x")
    kills, logs, sleeps = _recorder()
    br.execute_reap(
        [c],
        dry_run=False,
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        is_alive_fn=lambda pid: True,  # survives SIGTERM
        log_fn=logs.append,
        sleep_fn=sleeps.append,
    )
    assert kills == [(311, signal.SIGTERM), (311, signal.SIGKILL)]
    assert len(logs) == 2
    assert logs[1].startswith("browser-reap: KILL pid=311")
    assert "signal=SIGKILL" in logs[1]


def test_format_journal_line_bounds_cmd_and_uses_real_hour_math():
    """The journal caps cmd at 197 chars + '...' and computes age by a
    true /3600 — a 65000s-old process is 18.06h, not 18.05h."""
    long_cmd = "x" * 300
    c = br.ReapCandidate(pid=320, age_seconds=65000.0, signature="sig", cmd=long_cmd)
    line = br.format_journal_line(c, dry_run=True)
    assert "age=18.06h" in line
    assert "x" * 197 + "..." in line
    assert "x" * 198 + "..." not in line


# ---------------------------------------------------------------------------
# Second pass: constants, boundary, process table defaults, liveness probe
# ---------------------------------------------------------------------------

def test_grace_seconds_is_pinned_at_three():
    """SIGTERM→SIGKILL grace mirrors the orchestration reaper at exactly
    3 seconds."""
    assert br.GRACE_SECONDS == 3


def test_format_journal_line_keeps_exactly_200_char_cmd():
    """The cmd cap is inclusive at 200: a 200-char cmd stays whole, a
    201-char cmd truncates to 197 + '...'."""
    exact = "x" * 200
    line = br.format_journal_line(
        br.ReapCandidate(pid=1, age_seconds=7200.0, signature="sig", cmd=exact),
        dry_run=True,
    )
    assert exact in line

    over = "y" * 201
    line2 = br.format_journal_line(
        br.ReapCandidate(pid=1, age_seconds=7200.0, signature="sig", cmd=over),
        dry_run=True,
    )
    assert "y" * 197 + "..." in line2
    assert "y" * 201 not in line2


def test_collect_procs_defaults_missing_ppid_and_create_time(monkeypatch):
    """A process whose ppid/create_time the kernel hides gets the 0/0.0
    defaults — never None (which would crash the age math), and the
    reaper's own pid stays excluded."""
    import os
    import sys

    class _FakeProc:
        def __init__(self, info):
            self.info = info

    class _FakePsutil:
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})

        @staticmethod
        def process_iter(attrs):
            return [
                _FakeProc({
                    "pid": 424242,
                    "ppid": None,
                    "create_time": None,
                    "cmdline": ["chrome", "--headless"],
                }),
                _FakeProc({
                    "pid": os.getpid(),
                    "ppid": 1,
                    "create_time": 1.0,
                    "cmdline": ["python", "x.py"],
                }),
            ]

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    procs = br.collect_procs()

    assert [p.pid for p in procs] == [424242]
    assert procs[0].ppid == 0
    assert procs[0].create_time == 0.0


def test_os_is_alive_probes_with_signal_zero(monkeypatch):
    """Liveness is os.kill(pid, 0) — signal 0 must never become a real
    signal (1 = SIGHUP would KILL the target), and ProcessLookupError
    means dead."""
    calls = []
    monkeypatch.setattr(br.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert br._os_is_alive(123) is True
    assert calls == [(123, 0)]

    def dead(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(br.os, "kill", dead)
    assert br._os_is_alive(123) is False
