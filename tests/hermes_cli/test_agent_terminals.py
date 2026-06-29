from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from collections.abc import Generator

import pytest

from hermes_cli.agent_terminals import CapabilityError, InvalidTarget, TmuxAgentSessionService


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


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
    assert any(w.window == "hermes" for w in service.list_windows("work"))

    service.send_keys("work", "hermes", "-hello-from-test")
    captured = service.capture("work", "hermes", start=-20)
    assert "fake hermes tui" in captured
    assert "-hello-from-test" in captured
    metadata = service.attach_metadata("work", "hermes")
    assert metadata["target"] == "work:hermes"
    attach_argv = metadata["attach_argv"]
    assert isinstance(attach_argv, list)
    assert attach_argv[-1] == "work:hermes"
    draft = service.handoff_draft("work", "hermes", start=-20)
    assert draft["target"] == "work:hermes"
    assert "## Recent pane capture" in str(draft["content"])
    service.interrupt("work", "hermes")

    log = (tmp_path / "agent-terminals" / "events.jsonl").read_text(encoding="utf-8")
    assert "hello-from-test" not in log
    assert "send_keys" in log
    assert "capture" in log
    assert "attach_metadata" in log
    assert "handoff_draft" in log


def test_ensure_existing_window_does_not_overwrite_process(tmp_path: Path, tmux_service: TmuxAgentSessionService) -> None:
    fake = _fake_hermes(tmp_path)
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_binary=fake, hermes_home=tmp_path)
    service._run("new-session", "-d", "-s", "work", "-n", "hermes", "sh", "-c", "printf existing-window; sleep 60")
    time.sleep(0.2)

    ensured = service.ensure("hermes")
    assert ensured.command in {"sh", "sleep"}
    assert "existing-window" in service.capture("work", "hermes")


def test_baseline_non_hermes_windows_are_not_created(tmp_path: Path, tmux_service: TmuxAgentSessionService) -> None:
    service = TmuxAgentSessionService(socket_path=tmux_service.socket_path, hermes_home=tmp_path)
    with pytest.raises(CapabilityError):
        service.ensure("claude")
