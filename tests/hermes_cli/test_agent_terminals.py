from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from collections.abc import Generator

import pytest

import hermes_cli.agent_terminals as agent_terminals
from hermes_cli.agent_terminals import (
    AgentTerminalError,
    CapabilityError,
    InvalidTarget,
    TmuxAgentSessionService,
    classify_agent_pane,
    strip_ansi,
)
from hermes_cli.projects_overview import ProjectEntry, ProjectsRegistry


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


@pytest.fixture(autouse=True)
def reset_workdir_options_cache() -> Generator[None, None, None]:
    agent_terminals._reset_workdir_options_cache()
    yield
    agent_terminals._reset_workdir_options_cache()


@pytest.fixture
def tmux_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TmuxAgentSessionService, None, None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    socket = tmp_path / "tmux.sock"
    service = TmuxAgentSessionService(socket_path=socket, hermes_home=home)
    yield service
    subprocess.run(["tmux", "-S", str(socket), "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _fake_hermes(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "hermes"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nprintf 'fake hermes tui\\n'\nsleep 60\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_validate_name_rejects_tmux_option_and_shell_payload() -> None:
    service = TmuxAgentSessionService()
    for value in ("-t", "work;kill-server", "work:bad", "../work", ""):
        with pytest.raises(InvalidTarget):
            service.validate_name(value)


def test_run_raises_on_tmux_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tmux = tmp_path / "fake-tmux"
    fake_tmux.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8")
    fake_tmux.chmod(fake_tmux.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        agent_terminals, "_TMUX_RUN_TIMEOUT_SECONDS", 0.05, raising=False
    )
    service = TmuxAgentSessionService(tmux_binary=str(fake_tmux), hermes_home=tmp_path)

    started = time.monotonic()
    with pytest.raises(AgentTerminalError, match="tmux command timed out"):
        service._run("list-sessions", check=False)

    assert time.monotonic() - started < 0.5


def test_workdir_options_enumerate_registry_and_git_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "projekt"
    repo.mkdir()
    worktree = tmp_path / "worktrees" / "feature-one"
    worktree.mkdir(parents=True)
    free_worktree = home / ".hermes" / "worktrees" / "freier-wt"
    free_worktree.mkdir(parents=True)
    os.utime(worktree, (100, 100))
    os.utime(free_worktree, (200, 200))
    monkeypatch.setattr(
        agent_terminals,
        "load_projects_registry",
        lambda **_kwargs: ProjectsRegistry(
            projects=[ProjectEntry(slug="alpha", name="Alpha Projekt", repo_path=str(repo))]
        ),
    )
    calls: list[tuple[str, ...]] = []

    def fake_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        cwd = args[2]
        if args[3:] == ["worktree", "list", "--porcelain"] and cwd == str(repo):
            output = (
                f"worktree {repo}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n"
                f"worktree {worktree}\nHEAD {'b' * 40}\n"
                "branch refs/heads/feature/one\n\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        if args[3:] == ["rev-parse", "--git-dir"] and cwd == str(free_worktree):
            return subprocess.CompletedProcess(args, 0, stdout=".git\n", stderr="")
        if args[3:] == ["branch", "--show-current"] and cwd == str(free_worktree):
            return subprocess.CompletedProcess(args, 0, stdout="freie-branch\n", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="not a git repository")

    monkeypatch.setattr(agent_terminals, "_run_git", fake_git)

    options = TmuxAgentSessionService.workdir_options()

    assert options[0] == {
        "key": "home",
        "label": "Zuhause (~)",
        "path": str(home),
        "group": "standard",
    }
    assert options[1] == {
        "key": f"dir:{repo}",
        "label": "Alpha Projekt",
        "path": str(repo),
        "group": "projekt",
    }
    assert [option["path"] for option in options[2:]] == [str(free_worktree), str(worktree)]
    assert options[2]["label"] == "freier-wt · freie-branch"
    assert options[3]["label"] == "Alpha Projekt · feature/one"
    assert all(option["group"] == "worktree" for option in options[2:])
    assert not any(option["path"] == str(repo) and option["group"] == "worktree" for option in options)

    # TTL cache shares the expensive enumeration until the explicit test reset.
    first_call_count = len(calls)
    assert TmuxAgentSessionService.workdir_options() == options
    assert len(calls) == first_call_count
    agent_terminals._reset_workdir_options_cache()
    TmuxAgentSessionService.workdir_options()
    assert len(calls) > first_call_count


def test_resolve_workdir_accepts_only_enumerated_dir_keys_and_checks_live_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    option = {
        "key": f"dir:{allowed}",
        "label": "Allowed",
        "path": str(allowed),
        "group": "projekt",
    }
    monkeypatch.setattr(TmuxAgentSessionService, "workdir_options", staticmethod(lambda: [option]))

    assert TmuxAgentSessionService.resolve_workdir(f"dir:{allowed}") == (
        f"dir:{allowed}",
        allowed,
    )
    with pytest.raises(InvalidTarget, match="unknown workdir"):
        TmuxAgentSessionService.resolve_workdir(f"dir:{unknown}")

    allowed.rmdir()
    with pytest.raises(CapabilityError, match="workdir not available"):
        TmuxAgentSessionService.resolve_workdir(f"dir:{allowed}")


def test_static_workdir_keys_and_order_remain_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for relative in (
        (".hermes", "hermes-agent"),
        ("projects", "family-organizer"),
        ("orchestration",),
    ):
        home.joinpath(*relative).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        agent_terminals,
        "load_projects_registry",
        lambda **_kwargs: ProjectsRegistry(),
    )
    monkeypatch.setattr(
        agent_terminals,
        "_run_git",
        lambda args: subprocess.CompletedProcess(args, 1, stdout="", stderr="not a repo"),
    )

    options = TmuxAgentSessionService.workdir_options()

    assert [option["key"] for option in options] == [
        "home",
        "hermes-agent",
        "family-organizer",
        "orchestration",
    ]
    assert all(option["group"] == "standard" for option in options)


def test_worktree_enumeration_is_capped_at_fifteen_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        agent_terminals,
        "load_projects_registry",
        lambda **_kwargs: ProjectsRegistry(
            projects=[ProjectEntry(slug="repo", name="Repo", repo_path=str(repo))]
        ),
    )
    paths = [tmp_path / "worktrees" / f"wt-{index:02d}" for index in range(17)]
    for index, path in enumerate(paths):
        path.mkdir(parents=True)
        os.utime(path, (index, index))
    porcelain = f"worktree {repo}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n" + "".join(
        f"worktree {path}\nHEAD {'b' * 40}\nbranch refs/heads/b-{index}\n\n"
        for index, path in enumerate(paths)
    )

    def fake_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[2] == str(repo):
            return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="not a repo")

    monkeypatch.setattr(agent_terminals, "_run_git", fake_git)
    worktree_options = [
        option
        for option in TmuxAgentSessionService.workdir_options()
        if option["group"] == "worktree"
    ]

    assert len(worktree_options) == 15
    assert [option["path"] for option in worktree_options] == [
        str(path) for path in reversed(paths[2:])
    ]


def test_broken_or_transient_hermes_binary_reports_capability_state(tmp_path: Path) -> None:
    missing = tmp_path / "missing-hermes"
    service = TmuxAgentSessionService(hermes_binary=missing, hermes_home=tmp_path)
    caps = service.capabilities().to_dict()
    assert caps["hermes_tui_available"] is False
    assert "resolvable" in str(caps["reason"])

    worktree_binary = tmp_path / ".worktrees" / "task" / "venv" / "bin" / "hermes"
    worktree_binary.parent.mkdir(parents=True)
    worktree_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    worktree_binary.chmod(worktree_binary.stat().st_mode | stat.S_IXUSR)
    service = TmuxAgentSessionService(hermes_binary=worktree_binary, hermes_home=tmp_path)
    caps = service.capabilities().to_dict()
    assert caps["hermes_tui_available"] is False
    assert "transient worktree" in str(caps["reason"])


def test_missing_path_hermes_reports_unavailable_without_cwd_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    service = TmuxAgentSessionService(tmux_binary="tmux", hermes_home=tmp_path)

    with pytest.raises(CapabilityError, match="not found"):
        service.resolve_hermes_binary()

    caps = service.capabilities().to_dict()
    assert caps["hermes_tui_available"] is False
    assert caps["hermes_binary"] is None
    assert "not found" in str(caps["reason"])


def test_temp_tmux_lifecycle_capture_send_and_secret_safe_logging(tmp_path: Path, tmux_service: TmuxAgentSessionService) -> None:
    fake = _fake_hermes(tmp_path)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_binary=fake, hermes_home=tmp_path)

    created = service.ensure("hermes")
    assert created.session == "work"
    assert created.window == "hermes"
    assert created.cwd == str(Path.home())
    assert any(w.window == "hermes" for w in service.list_windows("work"))

    service.send_keys("work", "hermes", "-hello-from-test")
    captured = service.capture("work", "hermes", start=-20)
    assert "fake hermes tui" in captured
    assert "-hello-from-test" in captured
    metadata = service.attach_metadata("work", "hermes")
    assert metadata["target"] == "work:hermes"
    assert metadata["cwd"] == str(Path.home())
    attach_argv = metadata["attach_argv"]
    assert isinstance(attach_argv, list)
    assert attach_argv[-1] == "work:=hermes"
    draft = service.handoff_draft("work", "hermes", start=-20)
    assert draft["target"] == "work:hermes"
    assert f"- cwd: `{Path.home()}`" in str(draft["content"])
    assert "## Recent pane capture" in str(draft["content"])
    service.interrupt("work", "hermes")

    log = (tmp_path / "agent-terminals" / "events.jsonl").read_text(encoding="utf-8")
    assert "hello-from-test" not in log
    assert "send_keys" in log
    assert "capture" in log
    assert "attach_metadata" in log
    assert "handoff_draft" in log


def test_capture_pane_and_send_keys_to_pane(tmp_path: Path, tmux_service: TmuxAgentSessionService) -> None:
    """Pane-id addressed capture/send (P0b) with invalid-id rejection."""
    service = TmuxAgentSessionService(
        socket_path=tmux_service.socket_path, hermes_home=tmp_path
    )
    service._run(
        "new-session",
        "-d",
        "-s",
        "work",
        "-n",
        "pane-test",
        "sh",
        "-c",
        "printf 'ready\\n'; read -r x; printf 'GOT:%s\\n' \"$x\"; sleep 60",
    )
    time.sleep(0.3)
    info = service.show("work", "pane-test")
    pane_id = info.pane_id
    assert pane_id and pane_id.startswith("%")

    cap = service.capture_pane(pane_id, start=-20)
    assert "ready" in cap

    service.send_keys_to_pane(pane_id, "secret-token-xyz", enter=True)
    deadline = time.time() + 3.0
    saw = False
    while time.time() < deadline:
        if "GOT:secret-token-xyz" in service.capture_pane(pane_id, start=-20):
            saw = True
            break
        time.sleep(0.1)
    assert saw

    with pytest.raises(InvalidTarget):
        service.capture_pane("not-a-pane")
    with pytest.raises(InvalidTarget):
        service.send_keys_to_pane("%-1", "x")
    with pytest.raises(InvalidTarget):
        service.send_keys_to_pane("work:pane-test", "x")

    log = (tmp_path / "agent-terminals" / "events.jsonl").read_text(encoding="utf-8")
    assert "secret-token-xyz" not in log
    assert "capture_pane" in log
    assert "send_keys_to_pane" in log


def test_ensure_existing_window_does_not_overwrite_process(tmp_path: Path, tmux_service: TmuxAgentSessionService) -> None:
    fake = _fake_hermes(tmp_path)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_binary=fake, hermes_home=tmp_path)
    service._run("new-session", "-d", "-s", "work", "-n", "hermes", "sh", "-c", "printf existing-window; sleep 60")
    time.sleep(0.2)

    ensured = service.ensure("hermes")
    assert ensured.command in {"sh", "sleep"}
    assert "existing-window" in service.capture("work", "hermes")
    target = service._cmd_target("work", "hermes")
    assert service._run("show-options", "-w", "-v", "-t", target, "@hermes_kind").stdout.strip() == "hermes"
    assert service._run("show-options", "-w", "-v", "-t", target, "@hermes_workdir").stdout.strip() == "home"


def _fake_agent_cli(home: Path, name: str) -> Path:
    path = home / ".local" / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf 'fake {name} cli\\n'\nsleep 60\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_non_hermes_agent_without_binary_reports_capability_error(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    with pytest.raises(CapabilityError, match="CLI not found"):
        service.ensure("claude")


def test_ensure_spawns_claude_in_allowlisted_workdir(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()  # fixture points HOME at tmp
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    fo_dir = home / "projects" / "family-organizer"
    fo_dir.mkdir(parents=True)

    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    created = service.ensure("claude", "family-organizer")
    assert created.session == "work"
    assert created.window == "claude-fo"
    assert created.cwd == str(fo_dir)
    assert "fake claude cli" in service.capture("work", "claude-fo")

    with pytest.raises(InvalidTarget):
        service.ensure("claude", "not-a-workdir")
    with pytest.raises(CapabilityError, match="workdir not available"):
        service.ensure("claude", "orchestration")


def test_grok_uses_subscription_cli_and_grok_build_model(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    grok = home / ".npm-global" / "bin" / "grok"
    grok.parent.mkdir(parents=True)
    grok.write_text("#!/bin/sh\nprintf 'fake grok args: %s\\n' \"$*\"\nsleep 60\n", encoding="utf-8")
    grok.chmod(grok.stat().st_mode | stat.S_IXUSR)

    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    definition = service.definition_for("grok")

    assert definition.argv == (str(grok.resolve()), "--model", "grok-4.5")
    created = service.ensure("grok")
    assert created.window == "grok"
    assert "fake grok args: --model grok-4.5" in service.capture("work", "grok")
    assert service.identity_for("work", "grok") == ("grok", "home")
    assert service.capabilities().to_dict()["agents"]["grok"]["available"] is True


def test_respawn_and_kill_refuse_live_processes_and_recover_dead_panes(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    live = service.ensure("claude")
    assert live.window == "claude"
    with pytest.raises(CapabilityError, match="not marked dead"):
        service.respawn_dead("work", "claude")
    with pytest.raises(CapabilityError, match="not marked dead"):
        service.kill_dead("work", "claude")

    # Dead pane: remain-on-exit keeps the window around after the process exits.
    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("new-window", "-d", "-t", "work:", "-n", "codex", "sh -c 'exit 0'")
    time.sleep(0.3)
    dead = service.show("work", "codex")
    assert dead.dead or not dead.pid

    _fake_agent_cli(home, "codex")
    respawned = service.respawn_dead("work", "codex")
    assert respawned.window == "codex"
    assert respawned.pid
    assert not respawned.dead

    service._run("new-window", "-d", "-t", "work:", "-n", "kimi", "sh -c 'exit 0'")
    time.sleep(0.3)
    service.kill_dead("work", "kimi")
    assert not service.window_exists("work", "kimi")

    with pytest.raises(CapabilityError, match="not a dashboard-managed"):
        service._run("new-window", "-d", "-t", "work:", "-n", "scratch-thing", "sh -c 'exit 0'")
        time.sleep(0.3)
        service.respawn_dead("work", "scratch-thing")


def test_terminate_live_kills_only_dashboard_managed_live_windows(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    live = service.ensure("claude")
    service.terminate_live(live.session, live.window)
    assert not service.window_exists("work", "claude")

    # Dead pane: terminate_live is now idempotent and kills dead panes too
    # (stale frontend dead-flag used to route here and 503).
    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("new-window", "-d", "-t", "work:", "-n", "codex", "sh -c 'exit 0'")
    time.sleep(0.3)
    dead = service.show("work", "codex")
    assert dead.dead or not dead.pid
    service.terminate_live("work", "codex")
    assert not service.window_exists("work", "codex")

    service._run("new-window", "-d", "-t", "work:", "-n", "scratch-thing", "sleep 60")
    with pytest.raises(CapabilityError, match="not a dashboard-managed"):
        service.terminate_live("work", "scratch-thing")


def test_terminate_live_allow_external_kills_foreign_session(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allow_external=True closes any window on the socket (foreign session)."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    # Seed managed session so the socket is live, then add a foreign session.
    service.ensure("claude")
    service._run("new-session", "-d", "-s", "foreign-agent", "-n", "python3", "sleep 60")
    assert service.window_exists("foreign-agent", "python3")

    # Default path still refuses non-work sessions.
    with pytest.raises(CapabilityError, match="not a dashboard-managed"):
        service.terminate_live("foreign-agent", "python3")

    service.terminate_live("foreign-agent", "python3", allow_external=True)
    assert not service.window_exists("foreign-agent", "python3")

    # Idempotent second call.
    service.terminate_live("foreign-agent", "python3", allow_external=True)


def test_terminate_live_allow_external_kills_non_parseable_work_window(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allow_external=True closes non-identity windows in the work session."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    service.ensure("claude")
    service._run("new-window", "-d", "-t", "work:", "-n", "scratch-thing", "sleep 60")
    assert service.window_exists("work", "scratch-thing")

    with pytest.raises(CapabilityError, match="not a dashboard-managed"):
        service.terminate_live("work", "scratch-thing")

    service.terminate_live("work", "scratch-thing", allow_external=True)
    assert not service.window_exists("work", "scratch-thing")
    service.terminate_live("work", "scratch-thing", allow_external=True)


def test_terminate_live_already_killed_window_is_success(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    live = service.ensure("claude")
    service.terminate_live(live.session, live.window)
    assert not service.window_exists("work", "claude")

    # Already gone — must not raise (double-click / race).
    service.terminate_live("work", "claude")


def test_terminate_live_twice_is_success(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    live = service.ensure("claude")
    service.terminate_live(live.session, live.window)
    service.terminate_live(live.session, live.window)
    assert not service.window_exists("work", "claude")


def test_terminate_live_on_dead_pane_is_success(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    service.ensure("claude")  # seed the "work" session
    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("new-window", "-d", "-t", "work:", "-n", "codex", "sh -c 'exit 0'")
    time.sleep(0.3)
    dead = service.show("work", "codex")
    assert dead.dead or not dead.pid

    service.terminate_live("work", "codex")
    assert not service.window_exists("work", "codex")


def test_list_windows_managed_flag_true_for_spawned_false_for_foreign(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inventory marks dashboard-spawned windows managed; foreign stay visible as unmanaged.

    managed gates only the terminate UI affordance. kill_dead must still remove
    dead foreign panes (intentional cleanup — not gated by managed).
    """
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    spawned = service.ensure("claude")
    assert spawned.managed is True
    assert spawned.to_dict()["managed"] is True

    # Non-parseable name in the work session (same pattern as terminate guard tests).
    service._run("new-window", "-d", "-t", "work:", "-n", "scratch-thing", "sleep 60")
    # Window in a different session — terminate_live refuses non-work sessions.
    service._run("new-session", "-d", "-s", "other-agent", "-n", "python3", "sleep 60")

    listed = {f"{w.session}:{w.window}": w for w in service.list_windows()}
    assert listed["work:claude"].managed is True
    assert listed["work:scratch-thing"].managed is False
    assert listed["other-agent:python3"].managed is False
    assert listed["work:scratch-thing"].to_dict()["managed"] is False
    assert listed["other-agent:python3"].to_dict()["managed"] is False

    # show() uses the same managed rule (single-window, cheap).
    assert service.show("work", "scratch-thing").managed is False
    assert service.show("work", "claude").managed is True

    # kill_dead on a dead foreign window still works (managed only gates terminate).
    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("new-window", "-d", "-t", "work:", "-n", "foreign-dead", "sh -c 'exit 0'")
    time.sleep(0.3)
    dead_foreign = service.show("work", "foreign-dead")
    assert dead_foreign.managed is False
    assert dead_foreign.dead or not dead_foreign.pid
    service.kill_dead("work", "foreign-dead")
    assert not service.window_exists("work", "foreign-dead")


def test_kill_dead_nonexistent_window_is_success(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    # No session / window at all — must not raise CapabilityError via show().
    service.kill_dead("work", "ghost-window")


def test_terminate_live_kill_window_toctou_already_gone_is_success(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kill-window CalledProcessError + window already gone → success, not 500."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    real_run = TmuxAgentSessionService._run
    calls: list[tuple[str, ...]] = []

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args and args[0] == "kill-window":
            raise subprocess.CalledProcessError(1, args, output="", stderr="can't find window")
        return real_run(self, *args, check=check)

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)

    # Pre-kill checks see the window; post-kill re-check reports gone (concurrent closer).
    def fake_exists(self: TmuxAgentSessionService, session: str, window: str) -> bool:
        if any(c and c[0] == "kill-window" for c in calls):
            return False
        return True

    monkeypatch.setattr(TmuxAgentSessionService, "window_exists", fake_exists)

    service.terminate_live("work", "claude")
    assert any(call and call[0] == "kill-window" for call in calls)


def test_kill_dead_kill_window_toctou_already_gone_is_success(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    # Force dead classification via display-message fixture, then fail kill-window.
    stdout = f"work\tclaude\t1\t%1\t12345\t1\tsh\t1751500000\t\t{home}\n"
    real_run = TmuxAgentSessionService._run
    calls: list[tuple[str, ...]] = []

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args and args[0] == "display-message":
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if args and args[0] == "kill-window":
            raise subprocess.CalledProcessError(1, args, output="", stderr="can't find window")
        return real_run(self, *args, check=check)

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    # window_exists: True until kill path re-checks after CalledProcessError.
    exists_calls = {"n": 0}
    real_exists = TmuxAgentSessionService.window_exists

    def fake_exists(self: TmuxAgentSessionService, session: str, window: str) -> bool:
        exists_calls["n"] += 1
        # First checks (pre-show / pre-kill) must see the window; post-kill re-check gone.
        if any(c and c[0] == "kill-window" for c in calls):
            return False
        return True

    monkeypatch.setattr(TmuxAgentSessionService, "window_exists", fake_exists)

    service.kill_dead("work", "claude")
    assert any(call and call[0] == "kill-window" for call in calls)
    # silence unused
    assert real_exists is not None
    assert exists_calls["n"] >= 1


def test_terminate_live_kill_window_still_present_raises_agent_terminal_error(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    real_run = TmuxAgentSessionService._run

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "kill-window":
            raise subprocess.CalledProcessError(1, args, output="", stderr="permission denied")
        return real_run(self, *args, check=check)

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    # Window still present after failed kill → AgentTerminalError (maps to 503, not 500).
    monkeypatch.setattr(TmuxAgentSessionService, "window_exists", lambda self, s, w: True)

    with pytest.raises(AgentTerminalError, match="failed to kill"):
        service.terminate_live("work", "claude")


def test_show_display_message_called_process_error_is_not_found(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    real_run = TmuxAgentSessionService._run

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "display-message":
            raise subprocess.CalledProcessError(1, args, output="", stderr="can't find pane")
        return real_run(self, *args, check=check)

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)

    with pytest.raises(CapabilityError, match="not found"):
        service.show("work", "claude")


def test_show_display_message_transient_error_raises_agent_terminal_error(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nit: non-gone display-message failures must not masquerade as not-found."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    real_run = TmuxAgentSessionService._run

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "display-message":
            raise subprocess.CalledProcessError(
                1, args, output="", stderr="error connecting to /tmp/tmux.sock"
            )
        return real_run(self, *args, check=check)

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)

    with pytest.raises(AgentTerminalError, match="display-message failed"):
        service.show("work", "claude")


def test_window_exists_gone_stderr_is_false(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: list-panes not-found messages → False (gone), not a raised error."""
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "list-panes":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="can't find window: ghost"
            )
        raise AssertionError(f"unexpected tmux args: {args}")

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    assert service.window_exists("work", "ghost") is False


def test_window_exists_transient_socket_error_raises(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: non-gone list-panes failures raise AgentTerminalError (honest 503).

    Bare "error connecting to socket" (no missing-file cold-start) must raise —
    not silently map to gone. Cold-start "error connecting … (No such file …)"
    remains gone so ensure() can spawn the first session.
    """
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "list-panes":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="error connecting to socket"
            )
        raise AssertionError(f"unexpected tmux args: {args}")

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    with pytest.raises(AgentTerminalError, match="list-panes failed"):
        service.window_exists("work", "claude")


def test_terminate_live_idempotent_when_list_panes_reports_gone(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1(a): close path sees gone via window_exists → success, no silent hang."""
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "list-panes":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="can't find window: claude"
            )
        raise AssertionError(f"unexpected tmux args for gone close: {args}")

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    # Already gone — must not raise.
    service.terminate_live("work", "claude")


def test_terminate_live_raises_on_transient_list_panes_failure(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1(b): socket/transient list-panes error must NOT become silent success."""
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "list-panes":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="error connecting to socket"
            )
        raise AssertionError(f"unexpected tmux args: {args}")

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    with pytest.raises(AgentTerminalError, match="list-panes failed"):
        service.terminate_live("work", "claude")


def test_kill_window_idempotent_stale_pane_id_is_noop_success(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: close carrying an old pane id must not kill a respawned same-name window."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    first = service.ensure("claude")
    old_pane_id = first.pane_id
    assert old_pane_id

    # Kill and recreate under the SAME name (respawn semantics) — new generation.
    service._run("kill-window", "-t", service._cmd_target("work", "claude"))
    second = service.ensure("claude")
    assert second.window == "claude"
    assert second.pane_id
    assert second.pane_id != old_pane_id

    # Stale close for the OLD pane: no-op success; new window must survive.
    ok = service._kill_window_idempotent("work", "claude", pane_id=old_pane_id)
    assert ok is True
    assert service.window_exists("work", "claude")
    still = service.show("work", "claude")
    assert still.pane_id == second.pane_id


def test_respawn_dead_refuses_foreign_session(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3: dead window in a non-work session must not be killed+recreated under work."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    # Seed a server (set-option -g needs a running server), then build a dead
    # pane in a foreign session under remain-on-exit.
    service.ensure("claude")
    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("new-session", "-d", "-s", "other-agent", "-n", "claude", "sh -c 'exit 0'")
    time.sleep(0.3)
    dead = service.show("other-agent", "claude")
    assert dead.dead or not dead.pid
    assert dead.session == "other-agent"

    with pytest.raises(CapabilityError, match="not a dashboard-managed"):
        service.respawn_dead("other-agent", "claude")
    # Foreign dead window must still exist (respawn must not have killed it).
    assert service.window_exists("other-agent", "claude")


def _patch_display_message(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> list[tuple[str, ...]]:
    """Force `show()`'s display-message call to return a crafted, tab-separated
    line (the real tmux output format) while every other tmux invocation still
    runs against the live socket. Returns the list of recorded `_run` calls so
    callers can assert kill-window was (not) reached."""
    calls: list[tuple[str, ...]] = []
    real_run = TmuxAgentSessionService._run

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args and args[0] == "display-message":
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return real_run(self, *args, check=check)

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    return calls


def test_respawn_and_kill_refuse_unparsable_pid_when_pane_not_marked_dead(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane whose pid field tmux can't be trusted to parse (blank/injected
    text) must still be refused if pane_dead never flipped to 1 — dead must be
    decided by the pane_dead flag, not by whether pid parsed as an int."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    stdout = f"work\tclaude\t1\t%1\tnot-a-pid\t0\tsh\t1751500000\t\t{home}\n"
    calls = _patch_display_message(monkeypatch, stdout)

    with pytest.raises(CapabilityError, match="not marked dead"):
        service.respawn_dead("work", "claude")
    with pytest.raises(CapabilityError, match="not marked dead"):
        service.kill_dead("work", "claude")

    assert not any(call and call[0] == "kill-window" for call in calls)


def test_create_new_always_spawns_fresh_window_and_numbers_collisions(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    fake = _fake_hermes(tmp_path)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_binary=fake, hermes_home=tmp_path)

    first = service.create_new("hermes")
    assert first.session == "work"
    assert first.window == "hermes"

    second = service.create_new("hermes")
    assert second.window == "hermes-2"
    assert service.window_exists("work", "hermes")
    assert service.window_exists("work", "hermes-2")

    third = service.create_new("hermes")
    assert third.window == "hermes-3"


def test_create_new_raises_when_all_numbered_slots_are_taken(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    fake = _fake_hermes(tmp_path)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_binary=fake, hermes_home=tmp_path)

    for _ in range(9):  # fills base "hermes" + "hermes-2" .. "hermes-9"
        service.create_new("hermes")

    with pytest.raises(CapabilityError, match="too many open"):
        service.create_new("hermes")


def test_identity_from_window_strips_numbered_collision_suffix() -> None:
    assert TmuxAgentSessionService._identity_from_window("claude-agent-2") == ("claude", "hermes-agent")
    assert TmuxAgentSessionService._identity_from_window("codex-3") == ("codex", "home")
    assert TmuxAgentSessionService._identity_from_window("hermes") == ("hermes", "home")
    assert TmuxAgentSessionService._identity_from_window("claude-fo-9") == ("claude", "family-organizer")


def test_dynamic_workdir_window_slug_handles_length_and_digit_suffix() -> None:
    assert TmuxAgentSessionService.window_name_for(
        "codex", "dir:/tmp/Feature Branch"
    ) == "codex-dir-feature-branch"
    assert TmuxAgentSessionService.window_name_for(
        "claude", "dir:/tmp/very-long-worktree-42"
    ) == "claude-dir-very-long-worktr"
    assert TmuxAgentSessionService.window_name_for(
        "hermes", "dir:/tmp/Sprint42"
    ) == "hermes-dir-sprint42x"


def test_identity_from_unknown_dynamic_window_falls_back_to_home() -> None:
    assert TmuxAgentSessionService._identity_from_window("codex-dir-feature-branch") == (
        "codex",
        "home",
    )
    assert TmuxAgentSessionService._identity_from_window("claude-dir-sprintx-2") == (
        "claude",
        "home",
    )


def test_respawn_dead_recovers_numbered_window_guard_still_blocks_live(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "codex")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    live_base = service.ensure("codex")
    assert live_base.window == "codex"
    # Simulate a numbered collision window (as create_new would leave behind
    # when "codex" is already taken).
    service._run("new-window", "-d", "-t", "work:", "-n", "codex-2", "sh -c 'sleep 60'")
    time.sleep(0.2)

    with pytest.raises(CapabilityError, match="not marked dead"):
        service.respawn_dead("work", "codex-2")
    with pytest.raises(CapabilityError, match="not marked dead"):
        service.kill_dead("work", "codex-2")

    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("kill-window", "-t", service._cmd_target("work", "codex-2"))
    service._run("new-window", "-d", "-t", "work:", "-n", "codex-2", "sh -c 'exit 0'")
    time.sleep(0.3)
    dead = service.show("work", "codex-2")
    assert dead.dead or not dead.pid

    respawned = service.respawn_dead("work", "codex-2")
    assert respawned.pid
    assert not respawned.dead
    # Respawn erhält den Namen: das tote codex-2 kommt als codex-2 zurück,
    # statt still aufs lebende Basis-Fenster umgeleitet zu werden.
    assert respawned.window == "codex-2"
    assert service.window_exists("work", "codex-2")


def _patch_list_windows_output(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> list[tuple[str, ...]]:
    """Force `list_windows()`'s tmux call to return a crafted, tab-separated
    line (the real `list-windows -F` output shape) while every other tmux
    invocation still runs against the live socket."""
    calls: list[tuple[str, ...]] = []
    real_run = TmuxAgentSessionService._run

    def fake_run(self: TmuxAgentSessionService, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args and args[0] == "list-windows":
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return real_run(self, *args, check=check)

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    return calls


def test_list_windows_parses_real_tab_separated_format_matches_create_new_base_name(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture line mirrors the literal `list-windows -F` shape tmux emits
    (tab-separated, pane_current_path last) — not a hand-built TmuxWindow —
    so a parsing regression would surface here. The parsed name is also the
    base name create_new's numbered-collision suffixing keys off of."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    fo_dir = home / "projects" / "family-organizer"
    fo_dir.mkdir(parents=True)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    fixture = f"work\tclaude-fo\t1\t%9\t9999\t0\tclaude\t1751500000\t\t{fo_dir}\n"
    calls = _patch_list_windows_output(monkeypatch, fixture)
    windows = service.list_windows("work")

    assert len(windows) == 1
    parsed = windows[0]
    assert parsed.session == "work"
    assert parsed.window == "claude-fo"
    assert parsed.active is True
    assert parsed.pane_id == "%9"
    assert parsed.pid == 9999
    assert parsed.command == "claude"
    assert parsed.cwd == str(fo_dir)
    assert parsed.dead is False
    assert parsed.activity == 1751500000
    assert parsed.window == service.window_name_for("claude", "family-organizer")

    first = service.create_new("claude", "family-organizer")
    assert first.window == parsed.window
    second = service.create_new("claude", "family-organizer")
    assert second.window == f"{parsed.window}-2"
    assert any(call and call[0] == "list-windows" for call in calls)


def test_kill_dead_kills_when_pane_dead_flag_set_even_with_pid_present(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pane_dead=1 is authoritative: a stale/racy pid field must not block the
    kill once tmux itself has flagged the pane dead."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    stdout = f"work\tclaude\t1\t%1\t12345\t1\tsh\t1751500000\t\t{home}\n"
    calls = _patch_display_message(monkeypatch, stdout)

    service.kill_dead("work", "claude")

    assert any(call and call[0] == "kill-window" for call in calls)
    assert not service.window_exists("work", "claude")


def test_spawn_sets_hermes_kind_and_workdir_window_options(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    fo_dir = home / "projects" / "family-organizer"
    fo_dir.mkdir(parents=True)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    service.ensure("claude", "family-organizer")

    target = service._cmd_target("work", "claude-fo")
    kind_proc = service._run("show-options", "-w", "-v", "-t", target, "@hermes_kind")
    workdir_proc = service._run("show-options", "-w", "-v", "-t", target, "@hermes_workdir")
    assert kind_proc.stdout.strip() == "claude"
    assert workdir_proc.stdout.strip() == "family-organizer"


def test_identity_for_prefers_window_options_over_name_parsing(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    assert service.identity_for("work", "claude") == ("claude", "home")


def test_set_window_identity_optionally_stamps_correlation_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TmuxAgentSessionService(socket_path=tmp_path / "tmux.sock", hermes_home=tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "_run", lambda *args, **_kwargs: calls.append(args))

    service._set_window_identity(
        "work",
        "claude",
        kind="claude",
        workdir_key="home",
        session_id="session-123",
        task_id="task-456",
    )

    target = "work:=claude"
    assert calls == [
        ("set-option", "-w", "-t", target, "@hermes_kind", "claude"),
        ("set-option", "-w", "-t", target, "@hermes_workdir", "home"),
        ("set-option", "-w", "-t", target, "@hermes_session_id", "session-123"),
        ("set-option", "-w", "-t", target, "@hermes_task_id", "task-456"),
    ]


def test_identity_for_falls_back_to_name_parsing_without_window_options(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    """A window created before @hermes_* options existed (no options ever
    set on it) must still resolve via the old name-based parsing."""
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service._run("new-session", "-d", "-s", "work", "-n", "codex", "sh -c 'sleep 60'")
    time.sleep(0.2)

    assert service.identity_for("work", "codex") == ("codex", "home")


def test_identity_for_falls_back_when_option_values_are_invalid(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    target = service._cmd_target("work", "claude")
    service._run("set-option", "-w", "-t", target, "@hermes_kind", "not-a-real-kind")
    service._run("set-option", "-w", "-t", target, "@hermes_workdir", "not-a-real-workdir")

    assert service.identity_for("work", "claude") == ("claude", "home")


def test_rename_happy_path_returns_window_with_new_name(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    renamed = service.rename("work", "claude", "my-claude")
    assert renamed.session == "work"
    assert renamed.window == "my-claude"
    assert service.window_exists("work", "my-claude")
    assert not service.window_exists("work", "claude")
    assert service.identity_for("work", "my-claude") == ("claude", "home")


def test_rename_rejects_collision_with_existing_window(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    _fake_agent_cli(home, "codex")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")
    service.ensure("codex")

    with pytest.raises(CapabilityError, match="already exists"):
        service.rename("work", "claude", "codex")


def test_rename_refuses_foreign_window(tmp_path: Path, tmux_service: TmuxAgentSessionService) -> None:
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service._run("new-session", "-d", "-s", "kimi-goal-test", "-n", "python3", "sh -c 'sleep 60'")
    time.sleep(0.2)

    with pytest.raises(CapabilityError, match="not a dashboard-managed"):
        service.rename("kimi-goal-test", "python3", "hijacked")


def test_rename_rejects_invalid_name(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    with pytest.raises(InvalidTarget):
        service.rename("work", "claude", "bad name!")


def test_respawn_dead_after_rename_uses_window_option_identity(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead window renamed to a custom name no longer matches
    `_identity_from_window`'s name parsing — respawn must still work because
    rename() stamps @hermes_* options that identity_for() reads back."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    _fake_agent_cli(home, "codex")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    # Start the "work" session via a live agent window, then create the dead
    # window directly (bypassing _spawn_window) so it starts with no
    # @hermes_* options — mirrors a window from before this patch.
    service.ensure("claude")
    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("new-window", "-d", "-t", "work:", "-n", "codex", "sh -c 'exit 0'")
    time.sleep(0.3)
    dead = service.show("work", "codex")
    assert dead.dead or not dead.pid

    renamed = service.rename("work", "codex", "my-custom-codex")
    assert renamed.window == "my-custom-codex"
    assert renamed.dead or not renamed.pid

    with pytest.raises(CapabilityError, match="not a dashboard-managed"):
        TmuxAgentSessionService._identity_from_window("my-custom-codex")

    respawned = service.respawn_dead("work", "my-custom-codex")
    assert respawned.window == "my-custom-codex"
    assert respawned.pid
    assert not respawned.dead


# ----- classify_agent_pane / strip_ansi -------------------------------------
# Fixtures below are copied VERBATIM from real `tmux capture-pane` output on
# the production system — do not "clean up" whitespace, it is load-bearing
# for the prompt-marker regexes.

_FIXTURE_A = (
    "──────────────────────────────────────────────────────────────────────────\n"
    "  [Fable 5] 30% verbraucht · 70% frei · 304k/1000k tok\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    "\n"
    "  ● main\n"
    "  ◯ builder  S5b Mobile-Dichte bauen             15m 13s · ↓ 283.0k tokens"
)

_FIXTURE_B = (
    "• Model changed to gpt-5.5 xhigh for Default mode.\n"
    "\n"
    "\n"
    "› Explain this codebase\n"
    "\n"
    "  gpt-5.5 xhigh · ~ · Main [default]"
)

_FIXTURE_C = (
    '   MCP server "vault-qmd" connected · 6 tools (stdio)\n'
    " ╭─────────────────────────────────────────────────────────────╮\n"
    " │ >                                                           │\n"
    " ╰─────────────────────────────────────────────────────────────╯\n"
    " yolo  K2.7 Code thinking  ~/.hermes/hermes-agent  main"
)

_FIXTURE_D = (
    " ─ ready │ gpt 5.5 │ 0 tok        ─ ….hermes/hermes-agent (main)\n"
    ' ❯ Try "write a test for…"'
)

_FIXTURE_E = "• Working (6m 27s • esc to interrupt) · 1 background terminal running"

_FIXTURE_F = "  Do you want to proceed?\n  ❯ 1. Yes\n    2. No, and tell Claude what to do differently"


def test_strip_ansi_removes_csi_sgr_and_osc_title_sequences() -> None:
    raw = "\x1b]0;window title\x07\x1b[1;32mgreen bold\x1b[0m plain \x1b[2Ktail"
    assert strip_ansi(raw) == "green bold plain tail"


def test_classify_agent_pane_dead_precedence_beats_running_signal() -> None:
    assert classify_agent_pane(_FIXTURE_E, 0.0, True) == "dead"
    assert classify_agent_pane(_FIXTURE_E, None, True) == "dead"


def test_classify_agent_pane_claude_permission_question_beats_everything() -> None:
    # "frage" is the strongest needs-me signal — it must win even paired with
    # a running-style signal in the same tail, at any age.
    assert classify_agent_pane(_FIXTURE_F, None, False) == "frage"
    assert classify_agent_pane(_FIXTURE_F, 5.0, False) == "frage"
    assert classify_agent_pane(_FIXTURE_F + "\n" + _FIXTURE_E, 5.0, False) == "frage"


def test_classify_agent_pane_codex_working_is_laeuft_regardless_of_age() -> None:
    assert classify_agent_pane(_FIXTURE_E, None, False) == "laeuft"
    assert classify_agent_pane(_FIXTURE_E, 9999.0, False) == "laeuft"


def test_classify_agent_pane_claude_subagent_fresh_activity_is_laeuft() -> None:
    # Regel 3: activity_age_s < 15 triggers "laeuft" regardless of markers;
    # the "◯ builder …" line alone is explicitly NOT a marker.
    assert classify_agent_pane(_FIXTURE_A, 5.0, False) == "laeuft"


def test_classify_agent_pane_claude_subagent_without_marker_falls_back_to_age() -> None:
    """Fixture A has no Regel-4-Marker: neither "● main" nor "◯ builder …"
    starts with ❯/›, contains "│ >" or "─ ready │". Without a marker, Regel 4/5
    ("wartet"/"idle" bei vorhandenem Marker) cannot fire — only Regel 6 (reines
    Alter) entscheidet. Ergebnis ist daher "laeuft"/"idle" je nach Alter, NICHT
    "wartet" (die Auftrags-Fixture-Notiz nannte "wartet" für den "sonst"-Fall;
    das ist ohne einen Marker in Fixture A nicht erreichbar — siehe Rückgabe)."""
    assert classify_agent_pane(_FIXTURE_A, 30.0, False) == "laeuft"
    assert classify_agent_pane(_FIXTURE_A, 300.0, False) == "idle"


def test_classify_agent_pane_codex_prompt_wartet_then_idle_by_age() -> None:
    assert classify_agent_pane(_FIXTURE_B, 120.0, False) == "wartet"
    assert classify_agent_pane(_FIXTURE_B, None, False) == "wartet"
    assert classify_agent_pane(_FIXTURE_B, 1800.0, False) == "idle"


def test_classify_agent_pane_kimi_box_prompt_wartet_then_idle_by_age() -> None:
    assert classify_agent_pane(_FIXTURE_C, 120.0, False) == "wartet"
    assert classify_agent_pane(_FIXTURE_C, 5000.0, False) == "idle"


def test_classify_agent_pane_hermes_tui_ready_wartet_then_idle_by_age() -> None:
    assert classify_agent_pane(_FIXTURE_D, 120.0, False) == "wartet"
    assert classify_agent_pane(_FIXTURE_D, 5000.0, False) == "idle"


def test_classify_agent_pane_empty_tail_falls_back_to_age_only_rule() -> None:
    assert classify_agent_pane("", 10.0, False) == "laeuft"
    assert classify_agent_pane("", 200.0, False) == "idle"
    assert classify_agent_pane("", None, False) == "idle"


def test_overview_returns_tail_state_ansi_stripped_for_multiple_windows(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    _fake_agent_cli(home, "codex")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    service.ensure("claude")
    service.ensure("codex")
    time.sleep(0.2)

    overview = service.overview(tail_lines=10)
    assert isinstance(overview["now"], int)
    windows = overview["windows"]
    assert isinstance(windows, list)
    assert len(windows) >= 2

    by_window = {entry["window"]: entry for entry in windows}
    assert {"claude", "codex"} <= set(by_window)
    for entry in windows:
        assert entry["state_source"] == "heuristic"
        assert entry["state"] in {"dead", "frage", "laeuft", "wartet", "idle"}
        assert "\x1b" not in (entry["tail"] or "")

    assert "fake claude cli" in (by_window["claude"]["tail"] or "")
    assert "fake codex cli" in (by_window["codex"]["tail"] or "")

    log = (tmp_path / "agent-terminals" / "events.jsonl").read_text(encoding="utf-8")
    assert "fake claude cli" not in log
    assert '"event": "overview"' in log


def test_overview_capture_does_not_log_per_window_capture_events(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    service.ensure("claude")

    service.overview()

    log = (tmp_path / "agent-terminals" / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "capture"' not in log
    assert '"event": "overview"' in log


def _tmux_show_option(service: TmuxAgentSessionService, session: str, option: str) -> str:
    proc = service._run("show-options", "-t", session, option, check=False)
    return proc.stdout.strip()


def test_spawn_window_sets_session_scoped_mouse_and_history_limit(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    fake = _fake_hermes(tmp_path)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_binary=fake, hermes_home=tmp_path)

    service.ensure("hermes")

    assert _tmux_show_option(service, "work", "mouse") == "mouse on"
    assert _tmux_show_option(service, "work", "history-limit") == "history-limit 10000"


def test_ensure_session_options_is_session_scoped_not_global(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    fake = _fake_hermes(tmp_path)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_binary=fake, hermes_home=tmp_path)
    service._run("new-session", "-d", "-s", "work")
    # A second, foreign session must stay untouched — options are set with
    # `-t <session>`, never `-g`.
    service._run("new-session", "-d", "-s", "other")

    service.ensure_session_options("work")

    assert _tmux_show_option(service, "work", "mouse") == "mouse on"
    assert _tmux_show_option(service, "other", "mouse") == ""


def test_ensure_session_options_swallows_failure_for_missing_session(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    # No "ghost" session exists yet — must not raise, only log.
    service.ensure_session_options("ghost")
    log = (tmp_path / "agent-terminals" / "events.jsonl").read_text(encoding="utf-8")
    assert "ensure_session_options_failed" in log
