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
