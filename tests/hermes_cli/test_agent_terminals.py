from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from collections.abc import Generator

import pytest

import hermes_cli.agent_terminals as agent_terminals
from hermes_cli.agent_terminals import (
    AgentTerminalError,
    AgentWindowDefinition,
    CapabilityError,
    InvalidTarget,
    PaneCaptureCache,
    TerminalLaunchContext,
    TmuxWindow,
    TmuxAgentSessionService,
    classify_agent_pane,
    strip_ansi,
)
from hermes_cli.projects_overview import ProjectEntry, ProjectsRegistry


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


@pytest.fixture(autouse=True)
def reset_workdir_options_cache() -> Generator[None, None, None]:
    agent_terminals._reset_workdir_options_cache()
    agent_terminals._reset_pane_capture_cache()
    agent_terminals.clear_cli_probe_cache()
    yield
    agent_terminals._reset_workdir_options_cache()
    agent_terminals._reset_pane_capture_cache()
    agent_terminals.clear_cli_probe_cache()


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
    service = TmuxAgentSessionService(hermes_home=tmp_path / "hermes")

    assert service.resolve_workdir(f"dir:{allowed}") == (
        f"dir:{allowed}",
        allowed,
    )
    with pytest.raises(InvalidTarget, match="unknown workdir"):
        service.resolve_workdir(f"dir:{unknown}")

    allowed.rmdir()
    with pytest.raises(CapabilityError, match="workdir not available"):
        service.resolve_workdir(f"dir:{allowed}")


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


def test_terminal_worktree_group_is_validated_and_independently_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TmuxAgentSessionService(hermes_home=tmp_path / "hermes")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    terminal_root = repo / ".worktrees" / "terminal"
    terminal_root.mkdir(parents=True)
    run_ids = [f"run{index:02d}" for index in range(17)]
    worktrees: list[Path] = []
    for index, run_id in enumerate(run_ids):
        worktree = terminal_root / run_id
        worktree.mkdir()
        worktrees.append(worktree)
        run_dir = service.terminal_runs_root() / run_id
        run_dir.mkdir(parents=True)
        manifest = run_dir / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "terminal_run_id": run_id,
                    "start_mode": "isolated_write",
                    "worktree_path": str(worktree),
                    "worktree_branch": f"terminal/{run_id}",
                }
            ),
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        os.utime(run_dir, (index, index))

    invalid_dir = service.terminal_runs_root() / "invalid01"
    invalid_dir.mkdir()
    invalid_manifest = invalid_dir / "manifest.json"
    invalid_manifest.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "terminal_run_id": "invalid01",
                "start_mode": "isolated_write",
                "worktree_path": str(worktrees[-1]),
            }
        ),
        encoding="utf-8",
    )
    invalid_manifest.chmod(0o600)
    os.utime(invalid_dir, (100, 100))

    porcelain = (
        f"worktree {repo}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n"
        + "".join(
            f"worktree {path}\nHEAD {'b' * 40}\nbranch refs/heads/terminal/{path.name}\n\n"
            for path in worktrees
        )
    )

    def fake_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args == ["git", "-C", str(repo), "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(
                args, 0, stdout=porcelain, stderr=""
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="not a repo")

    monkeypatch.setattr(agent_terminals, "_run_git", fake_git)
    normal = [
        {
            "key": f"dir:/normal/{index}",
            "label": f"normal-{index}",
            "path": f"/normal/{index}",
            "group": "worktree",
        }
        for index in range(15)
    ]
    monkeypatch.setattr(service, "workdir_options", lambda: list(normal))

    options = service.workdir_options_with_terminal()
    terminal_options = [
        option for option in options if option["group"] == "terminal_worktree"
    ]
    assert len([option for option in options if option["group"] == "worktree"]) == 15
    assert len(terminal_options) == 15
    assert [option["terminal_run_id"] for option in terminal_options] == list(
        reversed(run_ids[2:])
    )
    assert all(option["terminal_run_id"] != "invalid01" for option in terminal_options)


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
    assert "content" not in draft or draft.get("content") is None
    assert "## Recent pane capture" not in str(draft)
    # Wave-2 windows expose structured source; legacy windows may upgrade_required.
    assert draft.get("schema_version") == 1
    assert "upgrade_required" in draft or draft.get("terminal_run_id") or draft.get("capture")
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
    version_line = {
        "claude": "2.1.217 (Claude Code)",
        "codex": "codex-cli 0.145.0",
        "grok": "grok 0.2.106 (fake) [stable]",
        "qwen": "0.20.0",
        "kimi": "0.29.0",
    }.get(name, "0.0.0")
    help_text = {
        "claude": "  -r, --resume [value]\\n  --fork-session",
        "codex": "  resume          Resume a previous session\\n  fork            Fork a previous session",
        "grok": "  -r, --resume [<SESSION_ID>]\\n      --fork-session",
        "qwen": "  -r, --resume              Resume a specific session",
        "kimi": "  fresh only",
    }.get(name, "")
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then printf "%s\\n" "{version_line}"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then printf "%b\\n" "{help_text}"; exit 0; fi\n'
        f"printf 'fake {name} cli\\n'\n"
        "sleep 60\n",
        encoding="utf-8",
    )
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
    grok.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "grok 0.2.106 (fake)"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then printf "  -r, --resume [<SESSION_ID>]\\n  --fork-session\\n  -s, --session-id <SESSION_ID>\\n"; exit 0; fi\n'
        "printf 'fake grok args: %s\\n' \"$*\"\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    grok.chmod(grok.stat().st_mode | stat.S_IXUSR)

    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    definition = service.definition_for("grok")

    assert definition.argv[:3] == (str(grok.resolve()), "--model", "grok-4.5")
    assert definition.argv[3] == "--session-id"
    uuid.UUID(definition.argv[4])
    created = service.ensure("grok")
    assert created.window == "grok"
    assert "fake grok args: --model grok-4.5 --session-id" in service.capture(
        "work", "grok"
    )
    assert service.identity_for("work", "grok") == ("grok", "home")
    assert service.capabilities().to_dict()["agents"]["grok"]["available"] is True


def test_qwen_uses_qwen_code_cli(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    qwen = home / ".npm-global" / "bin" / "qwen"
    qwen.parent.mkdir(parents=True)
    qwen.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "0.20.0"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then printf "  -r, --resume Resume\\n"; exit 0; fi\n'
        "printf 'fake qwen args: %s\\n' \"$*\"\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    qwen.chmod(qwen.stat().st_mode | stat.S_IXUSR)

    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    definition = service.definition_for("qwen")

    # Qwen Code reads model + auth from ~/.qwen/settings.json, so the terminal
    # launches the bare CLI (generic branch — no server-side argv flags).
    assert definition.argv == (str(qwen.resolve()),)
    created = service.ensure("qwen")
    assert created.window == "qwen"
    assert "fake qwen args:" in service.capture("work", "qwen")
    assert service.identity_for("work", "qwen") == ("qwen", "home")
    assert service.capabilities().to_dict()["agents"]["qwen"]["available"] is True


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
    assert spawned.agent_kind == "claude"
    assert spawned.to_dict()["managed"] is True
    assert spawned.to_dict()["agent_kind"] == "claude"

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


def test_kill_dead_marks_terminal_manifest_ended(
    tmp_path: Path,
    tmux_service: TmuxAgentSessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    service = TmuxAgentSessionService(
        socket_path=tmux_service.socket_path, hermes_home=tmp_path
    )
    created = service.ensure("claude")
    terminal_run_id = service._read_window_option(
        "work", created.window, "@hermes_terminal_run_id"
    )
    assert terminal_run_id

    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("send-keys", "-t", created.pane_id, "C-c")
    time.sleep(0.3)
    assert service.show("work", created.window).dead
    service.kill_dead("work", created.window)

    manifest = service.read_terminal_manifest(terminal_run_id)
    assert manifest is not None
    assert manifest["status"] == "ended"
    assert isinstance(manifest.get("ended_at"), str)


def test_kill_dead_surfaces_manifest_lifecycle_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TmuxAgentSessionService(hermes_home=tmp_path)
    dead = TmuxWindow(
        "work",
        "claude",
        True,
        "%7",
        None,
        "sh",
        dead=True,
        managed=True,
        agent_kind="claude",
    )
    monkeypatch.setattr(service, "_show_if_present", lambda *_args: dead)
    monkeypatch.setattr(
        service,
        "_read_window_option",
        lambda *_args: "run-manifest-failure",
    )
    monkeypatch.setattr(service, "cleanup_related_isolated_attaches", lambda *_args: [])
    monkeypatch.setattr(service, "_kill_window_idempotent", lambda *_args, **_kwargs: True)

    def fail_update(_terminal_run_id: str, **_fields: object) -> dict[str, object]:
        raise OSError("injected manifest write failure")

    monkeypatch.setattr(service, "update_terminal_manifest", fail_update)
    with pytest.raises(OSError, match="injected manifest write failure"):
        service.kill_dead("work", "claude")


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


def test_execution_server_id_is_opaque_stable_and_socket_scoped(tmp_path: Path) -> None:
    first = TmuxAgentSessionService(
        socket_path=tmp_path / "one.sock", hermes_home=tmp_path
    )
    same = TmuxAgentSessionService(
        socket_path=tmp_path / "one.sock", hermes_home=tmp_path / "other-home"
    )
    other = TmuxAgentSessionService(
        socket_path=tmp_path / "two.sock", hermes_home=tmp_path
    )

    assert first.execution_server_id == same.execution_server_id
    assert first.execution_server_id != other.execution_server_id
    assert len(first.execution_server_id) == 64
    assert str(tmp_path) not in first.execution_server_id


def test_execution_correlation_reads_one_exact_window_option_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TmuxAgentSessionService(
        socket_path=tmp_path / "tmux.sock", hermes_home=tmp_path
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "@hermes_task_id t_123\n"
                "@hermes_run_id 17\n"
                "@hermes_correlation_id aabbccddeeff001122334455\n"
                "@unrelated ignored\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(service, "_run", fake_run)

    assert service.execution_correlation_for("work", "codex") == {
        "task_id": "t_123",
        "run_id": 17,
        "correlation_id": "aabbccddeeff001122334455",
    }
    assert calls == [("show-options", "-w", "-t", "work:=codex")]


def test_execution_correlation_stamp_is_pane_generation_cas_and_restorable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TmuxAgentSessionService(
        socket_path=tmp_path / "tmux.sock", hermes_home=tmp_path
    )
    current = TmuxWindow("work", "codex", True, "%7", 123, "node", str(tmp_path))
    previous = {
        "task_id": "t_old",
        "run_id": 3,
        "correlation_id": "00112233445566778899aabb",
    }
    writes: list[tuple[str, object | None]] = []
    monkeypatch.setattr(service, "show", lambda *_args: current)
    monkeypatch.setattr(
        service, "execution_correlation_for", lambda *_args: dict(previous)
    )
    monkeypatch.setattr(
        service,
        "_set_or_unset_window_option",
        lambda _session, _window, name, value: writes.append((name, value)),
    )
    monkeypatch.setattr(service, "_log_event", lambda *_args, **_kwargs: None)

    returned = service.stamp_execution_correlation(
        "work",
        "codex",
        expected_pane_id="%7",
        task_id="t_new",
        run_id=19,
        correlation_id="aabbccddeeff001122334455",
    )
    assert returned == previous
    assert writes == [
        ("@hermes_task_id", "t_new"),
        ("@hermes_run_id", 19),
        ("@hermes_correlation_id", "aabbccddeeff001122334455"),
    ]

    writes.clear()
    service.restore_execution_correlation(
        "work", "codex", previous, expected_pane_id="%7"
    )
    assert writes == [
        ("@hermes_task_id", "t_old"),
        ("@hermes_run_id", 3),
        ("@hermes_correlation_id", "00112233445566778899aabb"),
    ]

    writes.clear()
    service.restore_execution_correlation(
        "work",
        "codex",
        {"task_id": None, "run_id": None, "correlation_id": None},
        expected_pane_id="%7",
    )
    assert writes == [
        ("@hermes_task_id", None),
        ("@hermes_run_id", None),
        ("@hermes_correlation_id", None),
    ]

    monkeypatch.setattr(
        service,
        "show",
        lambda *_args: TmuxWindow("work", "codex", True, "%8", 456, "node"),
    )
    writes.clear()
    with pytest.raises(CapabilityError, match="changed pane generation"):
        service.stamp_execution_correlation(
            "work",
            "codex",
            expected_pane_id="%7",
            task_id="t_new",
            run_id=19,
            correlation_id="aabbccddeeff001122334455",
        )
    assert writes == []


def test_execution_correlation_survives_window_rename_in_temp_tmux(
    tmp_path: Path, tmux_service: TmuxAgentSessionService
) -> None:
    service = TmuxAgentSessionService(
        socket_path=tmux_service.socket_path, hermes_home=tmp_path
    )
    service._run(
        "new-session",
        "-d",
        "-s",
        "work",
        "-n",
        "codex",
        "sh",
        "-c",
        "sleep 60",
    )
    service._set_window_identity(
        "work", "codex", kind="codex", workdir_key="home"
    )
    before = service.show("work", "codex")
    service.stamp_execution_correlation(
        "work",
        "codex",
        expected_pane_id=before.pane_id,
        task_id="t_rename",
        run_id=23,
        correlation_id="11223344556677889900aabb",
    )

    renamed = service.rename("work", "codex", "codex-bound")
    assert renamed.pane_id == before.pane_id
    assert renamed.task_id == "t_rename"
    assert renamed.run_id == 23
    assert renamed.correlation_id == "11223344556677889900aabb"


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
    created = service.ensure("claude")
    terminal_run_id = service._read_window_option(
        "work", created.window, "@hermes_terminal_run_id"
    )
    assert terminal_run_id

    renamed = service.rename("work", "claude", "my-claude")
    assert renamed.session == "work"
    assert renamed.window == "my-claude"
    assert renamed.agent_kind == "claude"
    assert service.window_exists("work", "my-claude")
    assert not service.window_exists("work", "claude")
    assert service.identity_for("work", "my-claude") == ("claude", "home")
    manifest = service.read_terminal_manifest(terminal_run_id)
    assert manifest is not None
    assert manifest["window"] == "my-claude"

    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("send-keys", "-t", renamed.pane_id, "C-c")
    time.sleep(0.3)
    assert service.show("work", "my-claude").dead
    respawned = service.respawn_dead("work", "my-claude", action="fresh")
    assert respawned.window == "my-claude"
    assert not respawned.dead


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


def test_automatic_pane_snapshot_is_shared_across_services_and_single_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dashboard/poller readers share one canonical capture per pane activity."""
    clock = {"now": 100.0}
    cache = PaneCaptureCache(ttl_seconds=2.0, max_entries=8)
    socket = tmp_path / "shared.sock"
    first = TmuxAgentSessionService(
        socket_path=socket,
        hermes_home=tmp_path / "one",
        now=lambda: clock["now"],
        capture_cache=cache,
    )
    second = TmuxAgentSessionService(
        socket_path=socket,
        hermes_home=tmp_path / "two",
        now=lambda: clock["now"],
        capture_cache=cache,
    )
    calls: list[tuple[str, ...]] = []
    calls_lock = threading.Lock()

    def fake_run(
        _self: TmuxAgentSessionService, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        with calls_lock:
            calls.append(tuple(args))
        # Keep the leader in-flight long enough for the second reader to join it.
        time.sleep(0.05)
        return subprocess.CompletedProcess(args, 0, stdout="same pane\n", stderr="")

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(
            pool.map(
                lambda service: service.capture_pane_snapshot(
                    "%7", window_activity=90
                ),
                (first, second),
            )
        )

    assert [snapshot.raw for snapshot in snapshots] == ["same pane\n", "same pane\n"]
    assert len(calls) == 1
    assert calls[0][-2:] == ("-S", "-25")

    # A changed tmux activity generation is a new snapshot.
    second.capture_pane_snapshot("%7", window_activity=91)
    assert len(calls) == 2
    # Expiry also refreshes even when activity did not change.
    clock["now"] = 103.0
    first.capture_pane_snapshot("%7", window_activity=91)
    assert len(calls) == 3
    # Activity from the current second is intentionally never cached.
    first.capture_pane_snapshot("%7", window_activity=103)
    second.capture_pane_snapshot("%7", window_activity=103)
    assert len(calls) == 5
    # Explicit safety/recheck captures always bypass the automatic cache.
    first.capture_pane("%7", start=-25)
    assert len(calls) == 6


def test_inflight_snapshot_is_not_reused_after_send_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture completing after input was sent must not repopulate the cache."""
    cache = PaneCaptureCache(ttl_seconds=30.0, max_entries=8)
    service = TmuxAgentSessionService(
        socket_path=tmp_path / "shared.sock",
        hermes_home=tmp_path,
        now=lambda: 100.0,
        capture_cache=cache,
    )
    capture_started = threading.Event()
    release_capture = threading.Event()
    calls = 0

    def fake_run(
        _self: TmuxAgentSessionService, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            capture_started.set()
            assert release_capture.wait(timeout=2)
        return subprocess.CompletedProcess(
            args, 0, stdout=f"capture-{calls}\n", stderr=""
        )

    monkeypatch.setattr(TmuxAgentSessionService, "_run", fake_run)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            service.capture_pane_snapshot, "%7", window_activity=90
        )
        assert capture_started.wait(timeout=2)
        cache.invalidate_pane(service._capture_server_id, "%7")
        release_capture.set()
        assert pending.result(timeout=2).raw == "capture-1\n"

    refreshed = service.capture_pane_snapshot("%7", window_activity=90)
    assert refreshed.raw == "capture-2\n"
    assert calls == 2


def test_overview_uses_canonical_snapshot_and_clamps_requested_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = PaneCaptureCache(ttl_seconds=2.0, max_entries=8)
    service = TmuxAgentSessionService(
        socket_path=tmp_path / "overview.sock",
        hermes_home=tmp_path,
        now=lambda: 200.0,
        capture_cache=cache,
    )
    monkeypatch.setattr(
        service,
        "list_windows",
        lambda: [
            TmuxWindow(
                "work",
                "claude",
                True,
                "%8",
                123,
                "claude",
                activity=190,
            )
        ],
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        body = "\n".join(f"line-{i}" for i in range(1, 31)) + "\n"
        return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")

    monkeypatch.setattr(service, "_run", fake_run)

    short = service.overview(tail_lines=2)["windows"][0]["tail"]
    deep = service.overview(tail_lines=200)["windows"][0]["tail"]

    assert short == "line-29\nline-30"
    assert deep.startswith("line-6\n")
    assert deep.endswith("line-30")
    assert len(calls) == 1
    assert calls[0][-2:] == ("-S", "-25")


def test_send_keys_invalidates_automatic_pane_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = PaneCaptureCache(ttl_seconds=2.0, max_entries=8)
    service = TmuxAgentSessionService(
        socket_path=tmp_path / "invalidate.sock",
        hermes_home=tmp_path,
        now=lambda: 300.0,
        capture_cache=cache,
    )
    captures = 0

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal captures
        if args and args[0] == "capture-pane":
            captures += 1
        return subprocess.CompletedProcess(args, 0, stdout="pane\n", stderr="")

    monkeypatch.setattr(service, "_run", fake_run)
    service.capture_pane_snapshot("%9", window_activity=290)
    service.capture_pane_snapshot("%9", window_activity=290)
    assert captures == 1

    service.send_keys_to_pane("%9", "not-logged", enter=True)
    service.capture_pane_snapshot("%9", window_activity=290)
    assert captures == 2

    log = (tmp_path / "agent-terminals" / "events.jsonl").read_text(encoding="utf-8")
    assert "not-logged" not in log


def test_event_log_rotation_preserves_complete_jsonl_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_terminals, "_EVENT_LOG_MAX_BYTES", 180)
    service = TmuxAgentSessionService(hermes_home=tmp_path, now=lambda: 400.0)

    for seq in range(20):
        service._log_event("probe", seq=seq, bytes=32)

    log_dir = tmp_path / "agent-terminals"
    paths = [log_dir / "events.jsonl", log_dir / "events.jsonl.1", log_dir / "events.jsonl.2"]
    assert all(path.exists() for path in paths)
    decoded: list[dict[str, object]] = []
    for path in paths:
        data = path.read_text(encoding="utf-8")
        assert data.endswith("\n")
        decoded.extend(json.loads(line) for line in data.splitlines())
    assert any(record.get("seq") == 19 for record in decoded)
    assert not (log_dir / "events.jsonl.3").exists()


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


def test_build_agent_argv_closed_matrix_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_cli import agent_terminals as at

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    claude.chmod(0o755)
    grok = tmp_path / "grok"
    grok.write_text("#!/bin/sh\n", encoding="utf-8")
    grok.chmod(0o755)
    qwen = tmp_path / "qwen"
    qwen.write_text("#!/bin/sh\n", encoding="utf-8")
    qwen.chmod(0o755)
    # Seed probe allowlist so Resume/Fork stay available without live CLIs.
    for kind, path, actions in (
        ("codex", binary, {"fresh": True, "resume": True, "fork": True, "lean": False, "compact": False}),
        ("claude", claude, {"fresh": True, "resume": True, "fork": True, "lean": False, "compact": False}),
        ("grok", grok, {"fresh": True, "resume": True, "fork": True, "lean": False, "compact": False}),
        ("qwen", qwen, {"fresh": True, "resume": True, "fork": False, "lean": False, "compact": False}),
    ):
        at.seed_cli_probe_cache(kind, path, actions)

    service = at.TmuxAgentSessionService(hermes_home=tmp_path / "hh")
    assert service.build_agent_argv("codex", binary=binary, action="fresh") == (str(binary),)
    assert service.build_agent_argv(
        "codex", binary=binary, action="resume", native_session_id="sess-1"
    ) == (str(binary), "resume", "sess-1")
    assert service.build_agent_argv(
        "codex", binary=binary, action="fork", native_session_id="sess-1"
    ) == (str(binary), "fork", "sess-1")
    assert service.build_agent_argv("claude", binary=claude, action="fresh") == (str(claude),)
    # No global --continue fallback when native_session_id is missing.
    with pytest.raises(at.CapabilityError):
        service.build_agent_argv("claude", binary=claude, action="resume")
    assert service.build_agent_argv(
        "claude", binary=claude, action="resume", native_session_id="sess-c"
    ) == (str(claude), "--resume", "sess-c")
    assert service.build_agent_argv(
        "claude", binary=claude, action="fork", native_session_id="sess-c"
    ) == (str(claude), "--resume", "sess-c", "--fork-session")
    assert service.build_agent_argv("grok", binary=grok, action="fresh") == (
        str(grok),
        "--model",
        "grok-4.5",
    )
    assert service.build_agent_argv(
        "grok", binary=grok, action="fork", native_session_id="g1"
    ) == (str(grok), "--model", "grok-4.5", "--resume", "g1", "--fork-session")
    with pytest.raises(at.CapabilityError):
        service.build_agent_argv("qwen", binary=qwen, action="resume")
    assert service.build_agent_argv(
        "qwen", binary=qwen, action="resume", native_session_id="q1"
    ) == (str(qwen), "--resume", "q1")
    with pytest.raises(at.CapabilityError):
        service.build_agent_argv("kimi", binary=binary, action="resume")
    with pytest.raises(at.CapabilityError):
        service.build_agent_argv("claude", binary=claude, action="fork")
    with pytest.raises(at.CapabilityError):
        service.build_agent_argv("codex", binary=binary, action="fork")
    with pytest.raises(at.CapabilityError):
        service.build_agent_argv("qwen", binary=qwen, action="fork")
    with pytest.raises(at.CapabilityError):
        service.build_agent_argv("codex", binary=binary, context_profile="lean")


def test_build_agent_argv_lean_requires_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_cli import agent_terminals as at
    from hermes_cli import config as hermes_config

    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    at.seed_cli_probe_cache(
        "codex",
        binary,
        {"fresh": True, "resume": True, "fork": True, "lean": False, "compact": False},
    )
    service = at.TmuxAgentSessionService(hermes_home=tmp_path / "hh")
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "agent_terminals": {
                "lean_context_profiles": {
                    "codex": "operator-lean",
                    "claude": "must-stay-disabled",
                }
            }
        },
    )
    assert service.build_agent_argv("codex", binary=binary, context_profile="lean") == (
        str(binary),
        "-p",
        "operator-lean",
    )
    assert hermes_config.DEFAULT_CONFIG["agent_terminals"]["lean_context_profiles"] == {}


def test_write_terminal_manifest_free_mode_null_worktree(tmp_path: Path) -> None:
    service = TmuxAgentSessionService(hermes_home=tmp_path)
    launch = TerminalLaunchContext(
        terminal_run_id="run123",
        agent_kind="hermes",
        start_mode="free",
        context_profile="full",
        cwd=str(tmp_path),
        worktree_path=None,
        argv=(str(tmp_path / "hermes"), "--tui"),
    )
    path = service.write_terminal_manifest(launch, window="hermes-home", session="work")
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["worktree_path"] is None
    assert data["start_mode"] == "free"
    assert data["terminal_run_id"] == "run123"
    assert data["status"] == "running"
    run_dir = path.parent
    assert oct(run_dir.stat().st_mode & 0o777) == "0o700"


def test_capabilities_include_closed_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_cli import agent_terminals as at

    service = TmuxAgentSessionService(hermes_home=tmp_path)
    bins = {}
    for kind in ("claude", "codex", "grok", "qwen", "kimi", "hermes"):
        p = tmp_path / kind
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
        bins[kind] = p
    at.seed_cli_probe_cache("claude", bins["claude"], {"fresh": True, "resume": True, "fork": True, "lean": False, "compact": False})
    at.seed_cli_probe_cache("codex", bins["codex"], {"fresh": True, "resume": True, "fork": True, "lean": False, "compact": False})
    at.seed_cli_probe_cache("grok", bins["grok"], {"fresh": True, "resume": True, "fork": True, "lean": False, "compact": False})
    at.seed_cli_probe_cache("qwen", bins["qwen"], {"fresh": True, "resume": True, "fork": False, "lean": False, "compact": False})
    at.seed_cli_probe_cache("kimi", bins["kimi"], {"fresh": True, "resume": False, "fork": False, "lean": False, "compact": False})
    at.seed_cli_probe_cache("hermes", bins["hermes"], {"fresh": True, "resume": False, "fork": False, "lean": False, "compact": False})
    monkeypatch.setattr(service, "resolve_agent_binary", lambda kind: bins[kind])
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/tmux")
    caps = service.capabilities().to_dict()
    agents = caps["agents"]
    assert agents["claude"]["actions"]["resume"] is True
    assert agents["claude"]["actions"]["fork"] is True
    assert agents["claude"]["actions"]["lean"] is False
    assert agents["codex"]["actions"]["fork"] is True
    assert agents["grok"]["actions"]["resume"] is True
    assert agents["grok"]["actions"]["fork"] is True
    assert agents["qwen"]["actions"]["resume"] is True
    assert agents["qwen"]["actions"]["fork"] is False
    assert agents["kimi"]["actions"]["resume"] is False
    assert agents["kimi"]["actions"]["fork"] is False
    assert agents["codex"]["actions"]["lean"] is False  # not allowlisted by default


def _argv_logging_cli(home: Path, name: str) -> tuple[Path, Path]:
    """Fake agent CLI that logs argv and stays alive for tmux capture."""
    path = home / ".local" / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    log = home / f"{name}-argv.log"
    if log.exists():
        log.unlink()
    version_line = {
        "claude": "2.1.217 (Claude Code)",
        "codex": "codex-cli 0.145.0",
        "grok": "grok 0.2.106 (fake) [stable]",
        "qwen": "0.20.0",
        "kimi": "0.29.0",
    }.get(name, "0.0.0")
    help_text = {
        "claude": "  -r, --resume [value]\\n  --fork-session\\n  --session-id <uuid>",
        "codex": "  resume          Resume a previous session\\n  fork            Fork a previous session",
        "grok": "  -r, --resume [<SESSION_ID>]\\n      --fork-session\\n  -s, --session-id <SESSION_ID>",
        "qwen": "  -r, --resume              Resume a specific session",
        "kimi": "  fresh only",
    }.get(name, "")
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$1" == "--version" ]]; then printf "%s\\n" "{version_line}"; exit 0; fi\n'
        f'if [[ "$1" == "--help" ]]; then printf "%b\\n" "{help_text}"; exit 0; fi\n'
        f'printf "%s\\n" "$0" "$@" >> "{log}"\n'
        f"printf 'fake {name} ready\\n'\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, log


def test_fake_cli_e2e_fresh_resume_fork_argv_matrix(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8: Fake-CLI E2E proves Fresh/Resume/Fork argv per supported agent.

    Claude fork is only resume+session-id+--fork-session. No pane-byte session
    injection is performed for fork/resume (capture stays free of payload dumps).
    """
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)

    for kind in ("claude", "codex", "grok", "qwen"):
        binary, log = _argv_logging_cli(home, kind)
        service = TmuxAgentSessionService(
            socket_path=tmux_service.socket_path,
            hermes_home=tmp_path,
        )
        # Fresh
        definition, launch = service.definition_and_launch(kind, "home", action="fresh")
        window = f"{kind}-fresh"
        definition = AgentWindowDefinition(
            kind=definition.kind,
            session=definition.session,
            window=window,
            argv=definition.argv,
            cwd=definition.cwd,
            env=definition.env,
            workdir_key=definition.workdir_key,
        )
        if log.exists():
            log.unlink()
        service._spawn_window(definition, launch)
        # Give the fake CLI a moment to log argv.
        deadline = time.time() + 2.0
        while time.time() < deadline and not log.exists():
            time.sleep(0.05)
        assert log.exists(), f"{kind} fresh did not log argv"
        logged = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert logged[0] == str(binary)
        assert logged[1:] == list(definition.argv[1:])
        pane = service.capture("work", window)
        # No session-id payload dump into the pane for fresh.
        assert "sess-" not in pane
        service.terminate_live("work", window)

        # Resume (where supported)
        actions = service.agent_context_actions(kind)
        if actions.get("resume"):
            if log.exists():
                log.unlink()
            sid = f"sess-{kind}-resume"
            definition, launch = service.definition_and_launch(
                kind, "home", action="resume", native_session_id=sid
            )
            window = f"{kind}-resume"
            definition = AgentWindowDefinition(
                kind=definition.kind,
                session=definition.session,
                window=window,
                argv=definition.argv,
                cwd=definition.cwd,
                env=definition.env,
                workdir_key=definition.workdir_key,
            )
            service._spawn_window(definition, launch)
            deadline = time.time() + 2.0
            while time.time() < deadline and not log.exists():
                time.sleep(0.05)
            logged = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert logged == list(definition.argv)
            pane = service.capture("work", window)
            # Resume uses argv flags only — no pane-byte session dump.
            assert "SESSION_PAYLOAD" not in pane
            service.terminate_live("work", window)

        # Fork (where supported) — fail-closed without sid is unit-tested;
        # with sid, Claude must pair --resume + --fork-session.
        if actions.get("fork"):
            if log.exists():
                log.unlink()
            sid = f"sess-{kind}-fork"
            definition, launch = service.definition_and_launch(
                kind, "home", action="fork", native_session_id=sid
            )
            window = f"{kind}-fork"
            definition = AgentWindowDefinition(
                kind=definition.kind,
                session=definition.session,
                window=window,
                argv=definition.argv,
                cwd=definition.cwd,
                env=definition.env,
                workdir_key=definition.workdir_key,
            )
            if kind == "claude":
                assert definition.argv == (str(binary), "--resume", sid, "--fork-session")
            service._spawn_window(definition, launch)
            deadline = time.time() + 2.0
            while time.time() < deadline and not log.exists():
                time.sleep(0.05)
            logged = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert logged == list(definition.argv)
            pane = service.capture("work", window)
            # null pane-bytes contract: no injected session dump beyond CLI output
            assert "SESSION_PAYLOAD" not in pane
            assert "dumped-session-bytes" not in pane
            service.terminate_live("work", window)

    # kimi stays fail-closed for resume/fork
    _argv_logging_cli(home, "kimi")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    with pytest.raises(CapabilityError):
        service.definition_and_launch("kimi", "home", action="resume")
    with pytest.raises(CapabilityError):
        service.definition_and_launch("kimi", "home", action="fork", native_session_id="x")


def test_isolated_write_temp_git_e2e_preserves_dirty_and_ahead_on_terminate_and_prune(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-9: full isolated-write path — worktree under .worktrees/terminal/,
    manifest carries worktree/branch/base_sha, dirty+ahead survive terminate
    and the prune script.
    """
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")

    # Make HOME itself a git repo so workdir=home is a valid isolated base.
    subprocess.run(["git", "init", "-b", "main"], cwd=home, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=home, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=home, check=True)
    (home / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=home, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=home, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=home,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)

    created = service.create_new("claude", "home", start_mode="isolated_write")
    assert created.session == "work"
    assert created.cwd is not None

    # Read stamped terminal_run_id and verify worktree + manifest.
    run_id = service._read_window_option("work", created.window, "@hermes_terminal_run_id")
    assert run_id
    manifest = service.read_terminal_manifest(run_id)
    assert manifest is not None
    assert manifest["start_mode"] == "isolated_write"
    assert manifest["base_sha"] == base_sha
    assert manifest["worktree_path"]
    assert manifest["worktree_branch"]
    wt = Path(str(manifest["worktree_path"]))
    assert wt.is_dir()
    assert wt == home / ".worktrees" / "terminal" / run_id
    assert str(created.cwd) == str(wt)
    assert (wt / "tracked.txt").read_text(encoding="utf-8") == "base\n"

    # Make dirty + ahead commits in the isolated worktree.
    (wt / "tracked.txt").write_text("dirty-ahead\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=wt, check=True)
    subprocess.run(
        ["git", "commit", "-m", "ahead-commit"],
        cwd=wt,
        check=True,
        capture_output=True,
    )
    (wt / "untracked-note.txt").write_text("keep-me\n", encoding="utf-8")
    ahead_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ahead_sha != base_sha

    # Terminate must mark ended but keep the dirty/ahead worktree.
    service.terminate_live("work", created.window)
    ended = service.read_terminal_manifest(run_id)
    assert ended is not None
    assert ended["status"] == "ended"
    assert wt.is_dir()
    assert (wt / "untracked-note.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert (wt / "tracked.txt").read_text(encoding="utf-8") == "dirty-ahead\n"

    # Prune script must also keep dirty/ahead terminal worktrees.
    script = Path(__file__).resolve().parents[2] / "scripts" / "prune-stale-worktrees.sh"
    env = os.environ.copy()
    env.update(
        {
            "PRUNE_REPOS": str(home),
            "KANBAN_DB_PATH": str(tmp_path / "missing-kanban.db"),
            "HERMES_HOME": str(tmp_path),
            "MIN_AGE_HOURS": "0",
            "TERMINAL_PRUNE_MIN_AGE_SECONDS": "0",
        }
    )
    result = subprocess.run(
        ["bash", str(script), "--apply"],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert wt.is_dir(), result.stdout + result.stderr
    assert "kept" in result.stdout
    assert (wt / "untracked-note.txt").exists()
    assert (wt / "tracked.txt").read_text(encoding="utf-8") == "dirty-ahead\n"


def test_isolated_write_spawn_failure_keeps_owned_worktree(
    tmp_path: Path,
    tmux_service: TmuxAgentSessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed tmux spawn leaves one identified, recoverable worktree."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _fake_agent_cli(home, "claude")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    service = TmuxAgentSessionService(
        socket_path=tmux_service.socket_path, hermes_home=tmp_path
    )
    monkeypatch.setattr(
        service, "resolve_workdir", lambda workdir=None: ("home", repo)
    )
    definition, launch = service.definition_and_launch(
        "claude", "home", start_mode="isolated_write"
    )
    worktree = Path(str(launch.worktree_path))
    prepared = service.read_terminal_manifest(launch.terminal_run_id)
    assert prepared is not None
    assert prepared["status"] == "prepared"
    assert prepared["worktree_path"] == str(worktree)
    assert worktree.is_dir()
    assert service._manifest_path(launch.terminal_run_id).stat().st_mode & 0o777 == 0o600

    real_run = service._run

    def fail_new_window(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "new-window":
            raise AgentTerminalError("injected new-window failure")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(service, "_run", fail_new_window)
    with pytest.raises(AgentTerminalError, match="injected new-window failure"):
        service._spawn_window(definition, launch)

    failed = service.read_terminal_manifest(launch.terminal_run_id)
    assert failed is not None
    assert failed["status"] == "spawn_failed"
    assert failed["terminal_run_id"] == launch.terminal_run_id
    assert failed["worktree_path"] == str(worktree)
    assert worktree.is_dir()
    assert not service.window_exists("work", definition.window)


def test_isolated_write_validates_binary_before_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    service = TmuxAgentSessionService(hermes_home=tmp_path / "hermes-home")
    monkeypatch.setattr(
        service, "resolve_workdir", lambda workdir=None: ("home", repo)
    )

    def unavailable(_kind: str) -> Path:
        raise CapabilityError("injected unavailable CLI")

    monkeypatch.setattr(service, "resolve_agent_binary", unavailable)
    with pytest.raises(CapabilityError, match="injected unavailable CLI"):
        service.definition_and_launch(
            "claude", "home", start_mode="isolated_write"
        )
    assert not (repo / ".worktrees" / "terminal").exists()
    assert not service.terminal_runs_root().exists()


def test_cli_probe_fail_closed_incompatible_major_and_missing_help(tmp_path: Path) -> None:
    from hermes_cli import agent_terminals as at

    at.clear_cli_probe_cache()
    good = tmp_path / "claude-good"
    good.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.217 (Claude Code)"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then printf "  -r, --resume [value]\\n  --fork-session\\n  --session-id <uuid>\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    good.chmod(0o755)
    unknown = tmp_path / "claude-unknown"
    unknown.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "3.0.0 (Claude Code)"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then printf "  -r, --resume [value]\\n  --fork-session\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    unknown.chmod(0o755)
    too_old = tmp_path / "claude-too-old"
    too_old.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.216 (Claude Code)"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then printf "  -r, --resume [value]\\n  --fork-session\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    too_old.chmod(0o755)
    no_fork = tmp_path / "claude-nofork"
    no_fork.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.217 (Claude Code)"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then printf "  -r, --resume [value]\\n"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    no_fork.chmod(0o755)

    good_actions = at.probe_agent_cli_actions("claude", good)
    assert good_actions["fresh"] is True
    assert good_actions["resume"] is True
    assert good_actions["fork"] is True
    assert good_actions["session_id"] is True
    assert good_actions["lean"] is False
    assert good_actions["compact"] is False

    unknown_actions = at.probe_agent_cli_actions("claude", unknown)
    assert unknown_actions["fresh"] is False
    assert unknown_actions["resume"] is False
    assert unknown_actions["fork"] is False
    assert at.probe_agent_cli_actions("claude", too_old)["fresh"] is False
    service = at.TmuxAgentSessionService(hermes_home=tmp_path / "hermes")
    with pytest.raises(at.CapabilityError, match="fresh start is not available"):
        service.build_agent_argv("claude", binary=unknown, action="fresh")

    no_fork_actions = at.probe_agent_cli_actions("claude", no_fork)
    assert no_fork_actions["fresh"] is True
    assert no_fork_actions["resume"] is True
    assert no_fork_actions["fork"] is False

    # Cache entries are adapter-specific even when two kinds resolve to the
    # exact same executable path.
    codex_on_claude_binary = at.probe_agent_cli_actions("codex", good)
    assert codex_on_claude_binary["fresh"] is False
    assert codex_on_claude_binary["resume"] is False
    assert codex_on_claude_binary["fork"] is False


@pytest.mark.parametrize(
    ("kind", "version_line", "help_text", "expected"),
    [
        (
            "claude",
            "2.9.0 (Claude Code)",
            "  -r, --resume [value]\\n  --fork-session\\n  --session-id <uuid>",
            {"resume": True, "fork": True, "session_id": True},
        ),
        (
            "codex",
            "codex-cli 0.999.0",
            "  resume          Resume a previous session\\n  fork            Fork a previous session",
            {"resume": True, "fork": True, "session_id": False},
        ),
        (
            "grok",
            "grok 0.99.0 (future) [stable]",
            "  -r, --resume [<SESSION_ID>]\\n      --fork-session\\n  -s, --session-id <SESSION_ID>",
            {"resume": True, "fork": True, "session_id": True},
        ),
        (
            "qwen",
            "0.99.0",
            "  -r, --resume              Resume a specific session",
            {"resume": True, "fork": False, "session_id": False},
        ),
        (
            "kimi",
            "0.99.0",
            "Usage: kimi [OPTIONS]",
            {"resume": False, "fork": False, "session_id": False},
        ),
    ],
)
def test_cli_probe_accepts_future_minor_updates_with_matching_help(
    tmp_path: Path,
    kind: str,
    version_line: str,
    help_text: str,
    expected: dict[str, bool],
) -> None:
    from hermes_cli import agent_terminals as at

    binary = tmp_path / kind
    binary.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{version_line}"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then printf "%b\\n" "{help_text}"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    actions = at.probe_agent_cli_actions(kind, binary)
    assert actions["fresh"] is True
    for action, available in expected.items():
        assert actions[action] is available


def test_resolve_workdir_rejects_non_manifest_terminal_path(tmp_path: Path) -> None:
    service = TmuxAgentSessionService(hermes_home=tmp_path / "hh")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    adversarial = repo / ".worktrees" / "terminal" / "evilpath"
    adversarial.mkdir(parents=True)
    with pytest.raises(InvalidTarget):
        service.resolve_workdir(f"dir:{adversarial}")


def test_respawn_dead_manifest_mismatch_refuses_before_kill(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10: mismatch vs 0600 manifest refuses before kill."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _argv_logging_cli(home, "claude")
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    definition, launch = service.definition_and_launch("claude", "home", action="fresh")
    definition = AgentWindowDefinition(
        kind=definition.kind,
        session=definition.session,
        window="claude-mm",
        argv=definition.argv,
        cwd=definition.cwd,
        env=definition.env,
        workdir_key=definition.workdir_key,
    )
    win = service._spawn_window(definition, launch)
    service._run("set-option", "-g", "remain-on-exit", "on")
    service._run("send-keys", "-t", win.pane_id, "C-c")
    time.sleep(0.3)
    assert service.show("work", "claude-mm").dead
    service.update_terminal_manifest(launch.terminal_run_id, agent_kind="codex")
    before = service.read_terminal_manifest(launch.terminal_run_id)
    assert before is not None
    with pytest.raises(CapabilityError, match="mismatch|refused"):
        service.respawn_dead("work", "claude-mm", action="fresh")
    assert service.window_exists("work", "claude-mm")
    after = service.read_terminal_manifest(launch.terminal_run_id)
    assert after is not None
    assert int(after.get("generation") or 1) == int(before.get("generation") or 1)


def test_respawn_dead_free_and_isolated_increment_generation(
    tmp_path: Path, tmux_service: TmuxAgentSessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10: dead Free/Isolated keep identity and one incremented generation."""
    home = Path.home()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _argv_logging_cli(home, "claude")
    hermes_home = tmp_path / "profiles" / "coder"
    hermes_home.mkdir(parents=True)
    env_home = tmp_path / "live-sentinel-should-not-be-used"
    env_home.mkdir()
    sentinel = env_home / "KEEP"
    sentinel.write_text("live\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(env_home))
    import hermes_constants

    context_home = tmp_path / "context-override"
    token = hermes_constants.set_hermes_home_override(context_home)
    try:
        service = TmuxAgentSessionService(
            socket_path=tmux_service.socket_path,
            hermes_home=hermes_home,
        )
        assert service.hermes_home == hermes_home
        assert service.terminal_runs_root() == tmp_path / "terminal-runs"
    finally:
        hermes_constants.reset_hermes_home_override(token)

    definition, launch = service.definition_and_launch("claude", "home", action="fresh")
    assert launch.native_session_id is not None
    free_native_before = launch.native_session_id
    free_window = "claude-free-r"
    definition = AgentWindowDefinition(
        kind=definition.kind,
        session=definition.session,
        window=free_window,
        argv=definition.argv,
        cwd=definition.cwd,
        env=definition.env,
        workdir_key=definition.workdir_key,
    )
    win = service._spawn_window(definition, launch)
    service._run("set-option", "-g", "remain-on-exit", "on")
    free_run = launch.terminal_run_id
    service._run("send-keys", "-t", win.pane_id, "C-c")
    time.sleep(0.3)
    assert service.show("work", free_window).dead
    m0 = service.read_terminal_manifest(free_run)
    assert m0 is not None
    assert int(m0.get("generation") or 1) == 1
    service.respawn_dead("work", free_window, action="fresh")
    m1 = service.read_terminal_manifest(free_run)
    assert m1 is not None
    assert m1["terminal_run_id"] == free_run
    assert int(m1["generation"]) == 2
    assert m1["start_mode"] == "free"
    assert m1["native_session_id"] != free_native_before
    assert isinstance(m1["native_session_id"], str)
    assert m1.get("worktree_path") is None
    assert (tmp_path / "terminal-runs" / free_run / "manifest.json").is_file()
    assert not (env_home / "terminal-runs" / free_run / "manifest.json").exists()
    assert not (context_home / "terminal-runs" / free_run / "manifest.json").exists()
    assert sentinel.read_text(encoding="utf-8") == "live\n"

    repo = tmp_path / "iso-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(service, "resolve_workdir", lambda workdir=None: ("home", repo))
    definition, launch = service.definition_and_launch(
        "claude", "home", action="fresh", start_mode="isolated_write"
    )
    assert launch.native_session_id is not None
    iso_native_before = launch.native_session_id
    iso_window = "claude-iso-r"
    definition = AgentWindowDefinition(
        kind=definition.kind,
        session=definition.session,
        window=iso_window,
        argv=definition.argv,
        cwd=definition.cwd,
        env=definition.env,
        workdir_key=definition.workdir_key,
    )
    win = service._spawn_window(definition, launch)
    iso_run = launch.terminal_run_id
    assert launch.worktree_path is not None
    service._run("send-keys", "-t", win.pane_id, "C-c")
    time.sleep(0.3)
    assert service.show("work", iso_window).dead
    m0 = service.read_terminal_manifest(iso_run)
    assert m0 is not None
    assert m0["start_mode"] == "isolated_write"
    assert Path(str(m0["worktree_path"])).is_dir()
    service.respawn_dead("work", iso_window, action="fresh")
    m1 = service.read_terminal_manifest(iso_run)
    assert m1 is not None
    assert m1["terminal_run_id"] == iso_run
    assert int(m1["generation"]) == 2
    assert m1["worktree_path"] == m0["worktree_path"]
    assert m1["native_session_id"] != iso_native_before
    assert isinstance(m1["native_session_id"], str)
    assert Path(str(m1["worktree_path"])).is_dir()
