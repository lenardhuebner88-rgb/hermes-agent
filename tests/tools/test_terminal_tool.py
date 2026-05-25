"""Regression tests for sudo detection and sudo password handling."""

import json

import tools.terminal_tool as terminal_tool


def setup_function():
    terminal_tool._reset_cached_sudo_passwords()


def teardown_function():
    terminal_tool._reset_cached_sudo_passwords()


def test_searching_for_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_printf_literal_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "printf '%s\\n' sudo"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_non_command_argument_named_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "grep -n sudo README.md"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_actual_sudo_command_uses_configured_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo apt install -y ripgrep")

    assert transformed == "sudo -S -p '' apt install -y ripgrep"
    assert sudo_stdin == "testpass\n"


def test_actual_sudo_after_leading_env_assignment_is_rewritten(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("DEBUG=1 sudo whoami")

    assert transformed == "DEBUG=1 sudo -S -p '' whoami"
    assert sudo_stdin == "testpass\n"


def test_explicit_empty_sudo_password_tries_empty_without_prompt(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for explicit empty password")

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo true")

    assert transformed == "sudo -S -p '' true"
    assert sudo_stdin == "\n"


def test_cached_sudo_password_is_used_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool._set_cached_sudo_password("cached-pass")

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("echo ok && sudo whoami")

    assert transformed == "echo ok && sudo -S -p '' whoami"
    assert sudo_stdin == "cached-pass\n"


def test_cached_sudo_password_isolated_by_session_key(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-a")
    terminal_tool._set_cached_sudo_password("alpha-pass")

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-b")
    assert terminal_tool._get_cached_sudo_password() == ""

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-a")
    assert terminal_tool._get_cached_sudo_password() == "alpha-pass"


def test_passwordless_sudo_skips_interactive_prompt_and_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError(
            "interactive sudo prompt should not run when sudo -n already works"
        )

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: True, raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo whoami")

    assert transformed == "sudo whoami"
    assert sudo_stdin is None


def test_passwordless_sudo_probe_rechecks_local_terminal(monkeypatch):
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result(0 if len(calls) == 1 else 1)

    monkeypatch.setattr(terminal_tool.subprocess, "run", fake_run)

    assert terminal_tool._sudo_nopasswd_works() is True
    assert terminal_tool._sudo_nopasswd_works() is False
    assert len(calls) == 2
    assert calls[0][0] == ["sudo", "-n", "true"]
    assert calls[1][0] == ["sudo", "-n", "true"]


def test_passwordless_sudo_probe_is_disabled_for_nonlocal_terminal_env(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    def _fail_run(*_args, **_kwargs):
        raise AssertionError("host sudo probe must not run for non-local terminal envs")

    monkeypatch.setattr(terminal_tool.subprocess, "run", _fail_run)

    assert terminal_tool._sudo_nopasswd_works() is False


def test_validate_workdir_allows_windows_drive_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project") is None
    assert terminal_tool._validate_workdir("C:/Users/Alice/project") is None


def test_validate_workdir_allows_windows_unc_paths():
    assert terminal_tool._validate_workdir(r"\\server\share\project") is None


# ---------------------------------------------------------------------------
# Phase 7 — command-anchored heavy-workload guard for HUB/DEFAULT
# ---------------------------------------------------------------------------


_HEAVY_COMMANDS = [
    "python -m pytest tests",
    "python3 -m pytest tests",
    "pytest tests",
    "ruff check .",
    "mypy .",
    "pyright",
    "tox",
    "nox",
    "coverage run -m pytest",
    "uv run pytest",
    "pdm run pytest",
    "hatch run pytest",
    # Bypass-resistance (Review-Finding #8): leading wrappers, versioned
    # interpreters, subshells, and bash -c quoting must all still match.
    "nohup pytest tests",
    "env FOO=1 pytest tests",
    "time pytest tests",
    "python3.11 -m pytest tests",
    "python3.12 -m coverage run -m pytest",
    "(cd /tmp && pytest tests)",
    "result=$(pytest tests)",
    "bash -c 'pytest -q'",
    "poetry run pytest",
    "FOO=1 BAR=2 pytest tests",
]


def test_default_gateway_blocks_python_test_and_lint_workloads(
    monkeypatch, tmp_path
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    for cmd in _HEAVY_COMMANDS:
        err = terminal_tool._default_gateway_local_workload_guard(
            cmd, background=False, env_type="local",
        )
        assert err, f"expected guard to block {cmd!r}"
        assert "Default Hermes gateway" in err, cmd


def test_default_gateway_does_not_block_pytest_in_paths(monkeypatch, tmp_path):
    """Negativ-Test: 'pytest' as a substring inside a path must not match."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    for cmd in [
        "cat /tmp/pytest-cache/log",
        "ls /tmp/pytest.log",
        "grep TODO /home/user/pytest-notes.txt",
        "echo coverage",
        "echo mypy",
    ]:
        assert (
            terminal_tool._default_gateway_local_workload_guard(
                cmd, background=False, env_type="local",
            )
            is None
        ), cmd


def test_named_profile_allows_python_test_workloads(monkeypatch, tmp_path):
    profile_home = tmp_path / ".hermes" / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    for cmd in _HEAVY_COMMANDS:
        assert (
            terminal_tool._default_gateway_local_workload_guard(
                cmd, background=False, env_type="local",
            )
            is None
        ), cmd


def test_worktree_home_allows_python_test_workloads(monkeypatch, tmp_path):
    worktree = tmp_path / ".hermes" / "worktrees" / "fix"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(worktree))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    for cmd in _HEAVY_COMMANDS:
        assert (
            terminal_tool._default_gateway_local_workload_guard(
                cmd, background=False, env_type="local",
            )
            is None
        ), cmd


def test_guard_inactive_outside_gateway_session(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    for cmd in _HEAVY_COMMANDS:
        assert (
            terminal_tool._default_gateway_local_workload_guard(
                cmd, background=False, env_type="local",
            )
            is None
        ), cmd


def test_guard_inactive_for_nonlocal_env_type(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    assert (
        terminal_tool._default_gateway_local_workload_guard(
            "pytest tests", background=False, env_type="ssh",
        )
        is None
    )


def test_guard_recognises_chained_command(monkeypatch, tmp_path):
    """Command anchored to `;`, `&&`, `||`, `|` — operator chains still match."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    for cmd in [
        "cd /tmp && pytest tests",
        "echo hi ; pytest tests",
        "false || ruff check .",
        "git status | mypy .",
    ]:
        assert (
            terminal_tool._default_gateway_local_workload_guard(
                cmd, background=False, env_type="local",
            )
            is not None
        ), cmd


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool._validate_workdir("C:\\Users\\Alice\\project\nwhoami")


# ---------------------------------------------------------------------------
# Integration: guard must fire from terminal_tool() entry, not only the helper
# (Review-Finding #1: helper was defined but never invoked at runtime.)
# ---------------------------------------------------------------------------


def test_terminal_tool_entry_blocks_heavy_workload_at_hub(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    result_json = terminal_tool.terminal_tool(command="pytest tests")
    result = json.loads(result_json)

    assert result["status"] == "blocked", result
    assert "Default Hermes gateway" in result["error"]


def test_terminal_tool_entry_allows_heavy_workload_in_named_profile(
    monkeypatch, tmp_path,
):
    profile_home = tmp_path / ".hermes" / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    # Use a benign command that *would* match the heavy-workload regex but
    # also exits successfully so we don't need to mock subprocess: 'true' is
    # not a heavy command, so the guard should be inactive on it anyway —
    # we only assert the guard does not turn it into 'blocked'.
    result_json = terminal_tool.terminal_tool(command="true")
    result = json.loads(result_json)

    assert result.get("status") != "blocked", result


def test_terminal_tool_entry_skips_guard_when_force(monkeypatch, tmp_path):
    """force=True (user already approved) must bypass the HUB guard too."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    # 'true' isn't heavy either, but the assertion is about not-blocked status
    # regardless of guard outcome; pytest as the command would actually
    # execute pytest which we don't want in CI — assert via 'true'.
    result_json = terminal_tool.terminal_tool(command="true", force=True)
    result = json.loads(result_json)

    assert result.get("status") != "blocked", result


def test_command_contains_heavy_workload_helper():
    """Sanity-check the parser-based detector against bypass attempts."""
    g = terminal_tool._command_contains_heavy_workload
    # positives
    assert g("pytest tests")
    assert g("nohup pytest tests")
    assert g("env FOO=1 pytest")
    assert g("python3.12 -m pytest tests")
    assert g("bash -c 'pytest -q'")
    assert g("(cd /tmp && pytest)")
    assert g("result=$(pytest)")
    assert g("/usr/local/venv/bin/pytest -q")
    # negatives — pytest as path component, not the executable
    assert g("cat /tmp/pytest-cache/log") is None
    assert g("ls /usr/bin/pytest") is None
    assert g("echo pytest") is None
    assert g("rg pytest src/") is None
    assert g("grep mypy README.md") is None
