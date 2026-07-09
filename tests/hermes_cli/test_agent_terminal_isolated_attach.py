from __future__ import annotations

import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from hermes_cli.agent_terminals import TmuxAgentSessionService

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TmuxAgentSessionService, None, None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    socket = tmp_path / "tmux.sock"
    item = TmuxAgentSessionService(socket_path=socket, hermes_home=home)
    subprocess.run(
        ["tmux", "-S", str(socket), "new-session", "-d", "-s", "work", "-n", "one", "sh", "-c", "while :; do sleep 60; done"],
        check=True,
    )
    subprocess.run(
        ["tmux", "-S", str(socket), "new-window", "-d", "-t", "work", "-n", "two", "sh", "-c", "while :; do sleep 60; done"],
        check=True,
    )
    yield item
    subprocess.run(["tmux", "-S", str(socket), "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _session_names(service: TmuxAgentSessionService) -> set[str]:
    result = subprocess.run(
        ["tmux", "-S", str(service.socket_path), "list-sessions", "-F", "#{session_name}"],
        check=True,
        text=True,
        capture_output=True,
    )
    return set(result.stdout.splitlines())


def test_create_isolated_attach_groups_source_and_hides_internal_session(service: TmuxAgentSessionService) -> None:
    target = service.create_isolated_attach("work", "two", attach_id="pane-a", now=1000)

    assert target.source_session == "work"
    assert target.source_window == "two"
    assert target.session == "__hermes_attach_pane-a"
    assert target.window == "two"
    assert _session_names(service) == {"work", "__hermes_attach_pane-a"}
    assert service.list_sessions() == ["work"]
    assert {item.window for item in service.list_windows()} == {"one", "two"}

    options = [
        subprocess.run(
            ["tmux", "-S", str(service.socket_path), "show-options", "-t", target.session, "-v", option],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        for option in (
            "@hermes_ephemeral_attach",
            "@hermes_attach_source",
            "@hermes_attach_window",
            "@hermes_attach_created_at",
        )
    ]
    assert options == ["1", "work", "two", "1000"]


def test_cleanup_isolated_attach_refuses_unmarked_prefix_session(service: TmuxAgentSessionService) -> None:
    subprocess.run(
        ["tmux", "-S", str(service.socket_path), "new-session", "-d", "-s", "__hermes_attach_user", "sh", "-c", "while :; do sleep 60; done"],
        check=True,
    )

    assert service.cleanup_isolated_attach("__hermes_attach_user") is False
    assert "__hermes_attach_user" in _session_names(service)
    assert "__hermes_attach_user" in service.list_sessions()
    assert any(item.session == "__hermes_attach_user" for item in service.list_windows())


def test_cleanup_related_and_stale_isolated_attaches(service: TmuxAgentSessionService) -> None:
    first = service.create_isolated_attach("work", "one", attach_id="first", now=1000)
    second = service.create_isolated_attach("work", "two", attach_id="second", now=1090)

    assert service.cleanup_stale_isolated_attaches(now=1120, grace_seconds=60) == [first.session]
    assert first.session not in _session_names(service)
    assert second.session in _session_names(service)

    assert service.cleanup_related_isolated_attaches("work", "two") == [second.session]
    assert second.session not in _session_names(service)


def test_cleanup_isolated_attach_is_idempotent(service: TmuxAgentSessionService) -> None:
    target = service.create_isolated_attach("work", "one", attach_id="idempotent", now=1000)

    assert service.cleanup_isolated_attach(target.session) is True
    assert service.cleanup_isolated_attach(target.session) is False
