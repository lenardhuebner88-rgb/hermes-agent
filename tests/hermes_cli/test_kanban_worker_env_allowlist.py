"""Regression tests for the kanban worker env allowlist (security hardening S1/S2).

Workers used to inherit the dispatcher's FULL environment via
``env = dict(os.environ)`` — including Discord bot tokens, the gateway's
``API_SERVER_KEY``, and provider keys for lanes the worker doesn't run.
A compromised / prompt-injected worker (running with
``--dangerously-skip-permissions``) could exfiltrate all of them.

The fix: ``_build_worker_env()`` copies only an allowlist into the child env.
Hermes-lane workers re-load their profile-scoped ``.env`` from disk at import
time (``load_hermes_dotenv`` with override), so stripping inherited secrets
does not starve them of their own lane credentials.

The claude-CLI worker path is stricter still: it never re-loads Hermes .env
files, runs on the Max subscription (must NOT see ``ANTHROPIC_API_KEY``),
and needs no LLM provider keys at all — so those are dropped there too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def captured_spawn(monkeypatch):
    """Patch Popen + profile resolution; return the capture dict."""
    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            self.pid = 12345

        def wait(self, *a, **kw):
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: _raise_fnf(),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda name: name,
    )
    return captured


def _raise_fnf():
    raise FileNotFoundError


def _spawn_default(captured, *, assignee="coder", **create_kw):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="env-allowlist", assignee=assignee, **create_kw)
        task = kb.get_task(conn, tid)
    kb._default_spawn(task, "/tmp/ws", board="default")
    return captured


SECRETS_THAT_MUST_NOT_LEAK = {
    "DISCORD_BOT_TOKEN": "discord-bot-secret",
    "DISCORD_BOT_TOKEN_REVIEWER": "discord-reviewer-secret",
    "API_SERVER_KEY": "gateway-api-secret",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "GITHUB_TOKEN": "gh-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "SOME_RANDOM_DAEMON_SECRET": "random-secret",
}

LANE_PROVIDER_KEYS = {
    "OPENROUTER_API_KEY": "sk-or-lane",
    "MINIMAX_API_KEY": "mm-lane",
    "KIMI_API_KEY": "sk-kimi-lane",
}


@pytest.fixture
def polluted_parent_env(monkeypatch):
    """Simulate a gateway parent env full of secrets + legit lane keys."""
    monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
    for key, val in {**SECRETS_THAT_MUST_NOT_LEAK, **LANE_PROVIDER_KEYS}.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("TERMINAL_TIMEOUT", "120")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")


def test_hermes_worker_env_strips_inherited_secrets(
    kanban_home, captured_spawn, polluted_parent_env
):
    _spawn_default(captured_spawn)
    env = captured_spawn["env"]
    leaked = {k for k in SECRETS_THAT_MUST_NOT_LEAK if k in env}
    assert not leaked, f"secrets leaked into worker env: {sorted(leaked)}"


def test_hermes_worker_env_keeps_contract_and_lane_keys(
    kanban_home, captured_spawn, polluted_parent_env
):
    _spawn_default(captured_spawn, max_iterations=42)
    env = captured_spawn["env"]
    # Kanban lifecycle contract — without these the worker cannot
    # complete/block its task or find the right board.
    for key in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_PROFILE",
        "HERMES_MAX_ITERATIONS",
    ):
        assert key in env, f"worker contract var {key} missing from child env"
    # System basics the child process needs to run at all.
    for key in ("PATH", "HOME", "LANG"):
        assert key in env, f"system var {key} missing from child env"
    # LLM lane keys pass through (the profile .env overrides them on load,
    # but inheritance is the safety net for profiles without a .env).
    for key in LANE_PROVIDER_KEYS:
        assert env.get(key) == LANE_PROVIDER_KEYS[key], f"lane key {key} lost"
    # Prefix passthrough (terminal knobs).
    assert env.get("TERMINAL_TIMEOUT") == "120"


def test_claude_worker_env_is_stricter(
    kanban_home, captured_spawn, polluted_parent_env, monkeypatch
):
    """The claude-CLI lane re-loads nothing from disk and runs on the Max
    subscription: no LLM provider keys, no bot tokens, no ANTHROPIC_API_KEY.
    """
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder")
    _spawn_default(captured_spawn)
    cmd = captured_spawn["cmd"]
    assert cmd and "claude" in cmd[0], f"expected claude-cli spawn, got {cmd}"
    env = captured_spawn["env"]
    leaked = {k for k in SECRETS_THAT_MUST_NOT_LEAK if k in env}
    assert not leaked, f"secrets leaked into claude worker env: {sorted(leaked)}"
    provider_leaked = {k for k in LANE_PROVIDER_KEYS if k in env}
    assert not provider_leaked, (
        f"LLM provider keys leaked into claude worker env: {sorted(provider_leaked)}"
    )
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "PATH", "HOME"):
        assert key in env, f"{key} missing from claude worker env"


def test_claude_worker_cmd_disallows_network_tools(
    kanban_home, captured_spawn, polluted_parent_env, monkeypatch
):
    """S2: the headless worker runs with --dangerously-skip-permissions, so
    tool restriction must go through --disallowedTools (deny wins even in
    bypass mode; an --allowedTools list would be a no-op there).
    """
    monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", "coder")
    _spawn_default(captured_spawn)
    cmd = captured_spawn["cmd"]
    assert "--disallowedTools" in cmd, f"--disallowedTools missing: {cmd}"
    tools = cmd[cmd.index("--disallowedTools") + 1]
    assert "WebFetch" in tools and "WebSearch" in tools, (
        f"network tools not denied: {tools!r}"
    )


def test_build_worker_env_allowlist_unit():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "HERMES_HOME": "/home/x/.hermes",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "LC_ALL": "C.UTF-8",
        "DISCORD_BOT_TOKEN": "leak",
        "API_SERVER_KEY": "leak",
        "RANDOM_UNRELATED": "leak",
        "OPENROUTER_API_KEY": "keep",
    }
    env = kb._build_worker_env(parent)
    assert set(env) == {
        "PATH", "HOME", "HERMES_HOME", "XDG_RUNTIME_DIR", "LC_ALL",
        "OPENROUTER_API_KEY",
    }
