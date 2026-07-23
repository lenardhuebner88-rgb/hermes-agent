from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str, body=None, scope_contract=None):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=body,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
        scope_contract=scope_contract,
    )


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def test_default_spawn_never_boots_the_tui(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "display:\n  interface: tui\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_TUI", "1")

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert "--cli" in captured["cmd"]
    assert "HERMES_TUI" not in captured["env"]


def test_default_spawn_goal_mode_uses_fully_quiet_query_path(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4244

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _make_task(kb, assignee="elias")
    task.goal_mode = True
    kb._default_spawn(task, str(workspace))

    assert "-Q" in captured["cmd"]


def test_default_spawn_intersects_structured_scope_contract_with_profile_toolsets(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - file
    - kanban
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    pid = kb._default_spawn(
        _make_task(kb, assignee="coder", scope_contract={"allowed_tools": ["kanban"]}),
        str(workspace),
    )

    assert pid == 4242
    assert captured["cmd"][captured["cmd"].index("--toolsets") + 1] == "kanban"


def test_default_spawn_ignores_out_of_baseline_scope_entries(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - kanban
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(
        str(profile),
        _make_task(
            kb,
            assignee="coder",
            scope_contract={"allowed_tools": ["terminal", "unknown", "file", "kanban"]},
        ),
    )

    assert resolved == ["terminal", "kanban"]


def test_default_spawn_pins_denied_toolset_for_empty_scope_contract(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - kanban
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    pid = kb._default_spawn(
        _make_task(kb, assignee="coder", scope_contract={"allowed_tools": []}),
        str(workspace),
    )

    assert pid == 4242
    assert "--toolsets" in captured["cmd"]
    assert (
        captured["cmd"][captured["cmd"].index("--toolsets") + 1]
        == kb._WORKER_SCOPE_DENIED_TOOLSET
    )


def test_resolve_worker_cli_toolsets_fails_closed_for_malformed_scope_contract(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - kanban
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    malformed_contracts = [
        "allowed_tools: [terminal]",
        {"allowed_tools": "terminal"},
        {"allowed_tools": ["terminal", 3]},
        {"unknown_scope_key": ["terminal"]},
    ]
    for contract in malformed_contracts:
        assert kb._resolve_worker_cli_toolsets(
            str(profile), _make_task(kb, assignee="coder", scope_contract=contract)
        ) == [kb._WORKER_SCOPE_DENIED_TOOLSET]


def test_resolve_worker_cli_toolsets_fails_closed_when_scope_allows_no_baseline_tools(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - kanban
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    assert kb._resolve_worker_cli_toolsets(
        str(profile),
        _make_task(kb, assignee="coder", scope_contract={"allowed_tools": ["web", "unknown"]}),
    ) == [kb._WORKER_SCOPE_DENIED_TOOLSET]


def test_default_spawn_does_not_parse_scope_contract_from_body_text(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - file
    - kanban
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    pid = kb._default_spawn(
        _make_task(kb, assignee="coder", body='scope_contract.allowed_tools=["kanban"]'),
        str(workspace),
    )

    assert pid == 4242
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    assert pinned == ["terminal", "file", "kanban"]


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_non_reviewer_worker_cli_toolsets_are_not_caged(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    for profile_name in ("coder", "researcher"):
        profile = root / "profiles" / profile_name
        profile.mkdir(parents=True)
        profile.joinpath("config.yaml").write_text(
            """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
            encoding="utf-8",
        )

        resolved = kb._resolve_worker_cli_toolsets(str(profile))

        assert resolved is not None
        assert "terminal" in resolved
        assert "web" in resolved
        assert "kanban" in resolved
        assert resolved != ["kanban"]


def test_reviewer_worker_cli_toolsets_are_verdict_only(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "reviewer"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - file
    - code_execution
    - delegation
    - skills
    - memory
    - session_search
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved == ["kanban", "review-read-only"]


def test_critic_worker_cli_toolsets_are_verdict_only(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "critic"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - file
    - code_execution
    - delegation
    - skills
    - memory
    - session_search
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved == ["kanban", "review-read-only"]


def test_verdict_read_only_profiles_are_all_caged_via_hermes_native_spawn(
    monkeypatch, tmp_path
):
    """Guards parity between the claude-cli and hermes-native spawn paths.

    ``_resolve_worker_cli_toolsets`` must cage every profile name in
    ``_CLAUDE_CLI_VERDICT_READ_ONLY_PROFILES`` to the verdict-only surface,
    so the two spawn paths can't drift apart if that set grows.
    """
    from hermes_cli import kanban_db as kb

    for profile_name in kb._CLAUDE_CLI_VERDICT_READ_ONLY_PROFILES:
        root = tmp_path / f".hermes-{profile_name}"
        profile = root / "profiles" / profile_name
        profile.mkdir(parents=True)
        profile.joinpath("config.yaml").write_text(
            """
platform_toolsets:
  cli:
    - terminal
    - web
    - file
    - code_execution
    - delegation
toolsets:
  - hermes-cli
""".lstrip(),
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(root))

        resolved = kb._resolve_worker_cli_toolsets(str(profile))

        assert resolved == ["kanban", "review-read-only"], profile_name


def test_reviewer_verdict_toolsets_keep_read_access_without_execution_tools(
    monkeypatch, tmp_path
):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "reviewer"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - file
    - code_execution
    - delegation
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")

    from hermes_cli import kanban_db as kb
    from model_tools import get_tool_definitions

    resolved = kb._resolve_worker_cli_toolsets(str(profile))
    tools = get_tool_definitions(enabled_toolsets=resolved, quiet_mode=True)
    names = {tool["function"]["name"] for tool in tools}

    assert "kanban_complete" in names
    assert "kanban_block" in names
    assert "kanban_show" in names
    assert "terminal" not in names
    assert "read_file" in names
    assert "search_files" in names
    assert "write_file" not in names
    assert "patch" not in names
    assert "execute_code" not in names
    assert "delegate_task" not in names


def test_reviewer_scope_contract_keeps_only_explicit_file_read_tools(
    monkeypatch, tmp_path
):
    """The active read-only posture survives a generic ``file`` deny."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "reviewer"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "toolsets:\n  - hermes-cli\ndisabled_toolsets:\n  - file\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")

    from hermes_cli import kanban_db as kb
    from model_tools import get_tool_definitions

    task = _make_task(
        kb,
        assignee="reviewer",
        scope_contract={
            "version": 2,
            "allowed_tools": ["read_file", "search_files"],
        },
    )
    resolved = kb._resolve_worker_cli_toolsets(str(profile), task)
    tools = get_tool_definitions(
        enabled_toolsets=resolved,
        disabled_toolsets=["file"],
        quiet_mode=True,
    )
    names = {tool["function"]["name"] for tool in tools}

    assert {"read_file", "search_files"} <= names
    assert "write_file" not in names
    assert "patch" not in names
    assert "terminal" not in names
    assert "execute_code" not in names
    assert "delegate_task" not in names


def test_generic_file_deny_still_wins_without_read_only_posture():
    """A generic deny must not reactivate ``file`` outside the explicit posture."""
    from model_tools import get_tool_definitions

    tools = get_tool_definitions(
        enabled_toolsets=["file"], disabled_toolsets=["file"], quiet_mode=True
    )
    names = {tool["function"]["name"] for tool in tools}

    assert "read_file" not in names
    assert "search_files" not in names
