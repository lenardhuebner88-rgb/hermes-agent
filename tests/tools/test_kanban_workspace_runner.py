"""Tests for the safe Kanban workspace command runner."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def worker_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_runner")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path))
    return workspace


def _run(args: dict) -> dict:
    from tools import kanban_workspace_runner as runner

    return json.loads(runner._handle_run_workspace_command(args))


def test_runner_requires_kanban_task_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))

    result = _run({"argv": ["python3", "probe.py"]})

    assert "error" in result
    assert "HERMES_KANBAN_TASK" in result["error"]


def test_runner_rejects_string_command(worker_workspace):
    result = _run({"argv": "python3 probe.py"})

    assert "error" in result
    assert "argv must be a list" in result["error"]


def test_runner_rejects_empty_argv(worker_workspace):
    result = _run({"argv": []})

    assert "error" in result
    assert "argv must not be empty" in result["error"]


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["bash", "-c", "python3 probe.py"], "command is not allowlisted"),
        (["sh", "-c", "python3 probe.py"], "command is not allowlisted"),
        (["python3", "-c", "print('x')"], "flag is not allowed"),
        (["python", "-m", "pytest"], "flag is not allowed"),
        (["curl", "https://example.com"], "command is not allowlisted"),
        (["env"], "command is not allowlisted"),
    ],
)
def test_runner_rejects_shells_inline_code_and_network_tools(worker_workspace, argv, expected):
    result = _run({"argv": argv})

    assert "error" in result
    assert expected in result["error"]


def test_runner_rejects_path_escape_argument(worker_workspace, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("print('nope')\n", encoding="utf-8")

    result = _run({"argv": ["python3", "../outside.py"]})

    assert "error" in result
    assert "outside workspace" in result["error"]


def test_runner_accepts_python_file_in_workspace(worker_workspace):
    (worker_workspace / "autonomy_probe.py").write_text(
        'def answer():\n    return "autonomy-ok"\n\nprint(answer())\n',
        encoding="utf-8",
    )

    result = _run({"argv": ["python3", "autonomy_probe.py"], "timeout_seconds": 10})

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "autonomy-ok\n"
    assert result["stderr"] == ""
    assert result["timed_out"] is False
    assert result["truncated"] is False
    assert result["cwd"] == str(worker_workspace.resolve())
    assert result["argv"] == ["python3", "autonomy_probe.py"]


def test_runner_ignores_untrusted_path_python3(worker_workspace, monkeypatch):
    (worker_workspace / "autonomy_probe.py").write_text(
        'print("autonomy-ok")\n',
        encoding="utf-8",
    )
    hijack = worker_workspace / "python3"
    hijack.write_text("#!/bin/sh\necho path-hijack\n", encoding="utf-8")
    hijack.chmod(hijack.stat().st_mode | 0o111)
    monkeypatch.setenv("PATH", str(worker_workspace))

    result = _run({"argv": ["python3", "autonomy_probe.py"]})

    assert result["ok"] is True
    assert result["stdout"] == "autonomy-ok\n"


def test_runner_blocks_scratch_workspace_outside_root(monkeypatch, tmp_path):
    root = tmp_path / "kanban-root"
    root.mkdir()
    workspace = tmp_path / "outside"
    workspace.mkdir()
    (workspace / "probe.py").write_text("print('nope')\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_runner")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE_KIND", "scratch")

    result = _run({"argv": ["python3", "probe.py"]})

    assert "error" in result
    assert "outside configured workspaces root" in result["error"]


def test_runner_allows_dir_workspace_outside_scratch_root(monkeypatch, tmp_path):
    root = tmp_path / "kanban-root"
    root.mkdir()
    workspace = tmp_path / "external-dir"
    workspace.mkdir()
    (workspace / "probe.py").write_text("print('dir-ok')\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_runner")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE_KIND", "dir")

    result = _run({"argv": ["python3", "probe.py"]})

    assert result["ok"] is True
    assert result["stdout"] == "dir-ok\n"


def test_runner_allows_worktree_workspace_outside_scratch_root(monkeypatch, tmp_path):
    root = tmp_path / "kanban-root"
    root.mkdir()
    workspace = tmp_path / "external-worktree"
    workspace.mkdir()
    (workspace / "probe.py").write_text("print('worktree-ok')\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_runner")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE_KIND", "worktree")

    result = _run({"argv": ["python3", "probe.py"]})

    assert result["ok"] is True
    assert result["stdout"] == "worktree-ok\n"


def test_runner_rejects_unknown_workspace_kind(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_runner")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE_KIND", "mystery")

    result = _run({"argv": ["python3", "probe.py"]})

    assert "error" in result
    assert "unknown workspace kind" in result["error"]


def test_runner_schema_hidden_without_kanban_task(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    import tools.kanban_workspace_runner  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}

    assert "kanban_run_workspace_command" not in names


def test_runner_schema_declares_tight_argv_shape(worker_workspace):
    import tools.kanban_workspace_runner  # ensure registered
    from tools.registry import registry

    schema = registry.get_entry("kanban_run_workspace_command").schema
    argv_schema = schema["parameters"]["properties"]["argv"]

    assert argv_schema["minItems"] == 2
    assert argv_schema["maxItems"] == 6
    assert "npm" in argv_schema["description"]
    assert "vitest" in argv_schema["description"]


def test_runner_accepts_registry_dispatch_kwargs(worker_workspace):
    from tools.registry import registry
    import tools.kanban_workspace_runner  # ensure registered

    (worker_workspace / "probe.py").write_text("print('dispatch-ok')\n", encoding="utf-8")

    result = json.loads(registry.dispatch(
        "kanban_run_workspace_command",
        {"argv": ["python3", "probe.py"]},
        task_id="t_runner",
    ))

    assert result["ok"] is True
    assert result["stdout"] == "dispatch-ok\n"


def test_runner_returns_ok_false_for_nonzero_exit(worker_workspace):
    (worker_workspace / "fail.py").write_text(
        "import sys\nprint('bad')\nsys.exit(3)\n",
        encoding="utf-8",
    )

    result = _run({"argv": ["python3", "fail.py"]})

    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert result["stdout"] == "bad\n"
    assert result["timed_out"] is False


def test_runner_truncates_large_output(worker_workspace):
    (worker_workspace / "large.py").write_text(
        "print('x' * 200)\n",
        encoding="utf-8",
    )

    result = _run({"argv": ["python3", "large.py"], "max_output_bytes": 20})

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= 20 + len("\n[truncated]\n".encode("utf-8"))


def test_runner_times_out_cleanly(worker_workspace):
    (worker_workspace / "slow.py").write_text(
        "import time\ntime.sleep(2)\nprint('late')\n",
        encoding="utf-8",
    )

    result = _run({"argv": ["python3", "slow.py"], "timeout_seconds": 1})

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["exit_code"] is None


def test_runner_accepts_pytest_file_in_workspace(worker_workspace):
    test_file = worker_workspace / "test_probe.py"
    test_file.write_text(
        "def test_probe():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    result = _run({"argv": ["pytest", "test_probe.py"], "timeout_seconds": 20})

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "1 passed" in result["stdout"]
    assert result["argv"] == ["pytest", "test_probe.py"]


def test_runner_pytest_returns_ok_false_for_failing_test(worker_workspace):
    test_file = worker_workspace / "test_fail.py"
    test_file.write_text(
        "def test_fail():\n    assert False\n",
        encoding="utf-8",
    )

    result = _run({"argv": ["pytest", "test_fail.py"], "timeout_seconds": 20})

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "FAILED" in result["stdout"]


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["pytest"], "requires a workspace-relative test path"),
        (["pytest", "-q"], "flag is not allowed"),
        (["pytest", "test_probe.py", "-q"], "too many arguments"),
        (["pytest", "../test_probe.py"], "test path is outside workspace"),
        (["pytest", "README.md"], "test extension is not allowlisted"),
    ],
)
def test_runner_pytest_rejects_unsafe_shapes(worker_workspace, tmp_path, argv, expected):
    (worker_workspace / "test_probe.py").write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    (worker_workspace / "README.md").write_text("# no\n", encoding="utf-8")
    (tmp_path / "test_probe.py").write_text("def test_outside():\n    assert False\n", encoding="utf-8")

    result = _run({"argv": argv})

    assert "error" in result
    assert expected in result["error"]


def test_runner_accepts_vitest_file_in_workspace(worker_workspace, monkeypatch):
    test_file = worker_workspace / "src/components/kitchen/WeekMobileList.test.tsx"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("import { describe, it } from 'vitest'\n", encoding="utf-8")
    monkeypatch.setenv("NPM_TOKEN", "secret-token")
    calls = {}

    def fake_run(run_argv, **kwargs):
        calls["argv"] = run_argv
        calls.update(kwargs)
        return subprocess.CompletedProcess(
            run_argv,
            0,
            stdout=b"vitest-ok\n",
            stderr=b"",
        )

    from tools import kanban_workspace_runner as runner

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = _run({
        "argv": [
            "npm",
            "exec",
            "vitest",
            "--",
            "run",
            "src/components/kitchen/WeekMobileList.test.tsx",
        ],
        "timeout_seconds": 20,
    })

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "vitest-ok\n"
    assert result["argv"] == [
        "npm",
        "exec",
        "vitest",
        "--",
        "run",
        "src/components/kitchen/WeekMobileList.test.tsx",
    ]
    assert calls["argv"] == result["argv"]
    assert calls["cwd"] == str(worker_workspace.resolve())
    assert calls["shell"] is False
    assert calls["stdin"] == subprocess.DEVNULL
    assert calls["env"]["HOME"] == str(worker_workspace.resolve())
    assert calls["env"]["npm_config_offline"] == "true"
    assert calls["env"]["npm_config_yes"] == "false"
    assert "NPM_TOKEN" not in calls["env"]


@pytest.mark.parametrize(
    "argv, expected",
    [
        (
            ["npm", "exec", "vitest", "--", "run", "../outside.test.ts"],
            "test path is outside workspace",
        ),
        (
            ["npm", "run", "vitest", "--", "run", "src/components/kitchen/WeekMobileList.test.tsx"],
            "npm command shape is not allowlisted",
        ),
        (
            ["npm", "exec", "jest", "--", "run", "src/components/kitchen/WeekMobileList.test.tsx"],
            "npm command shape is not allowlisted",
        ),
        (
            ["npm", "exec", "vitest", "--", "run", "src/components/kitchen/WeekMobileList.tsx"],
            "test extension is not allowlisted",
        ),
        (
            [
                "npm",
                "exec",
                "vitest",
                "--",
                "run",
                "src/components/kitchen/WeekMobileList.test.tsx",
                "--watch",
            ],
            "npm vitest command requires exactly",
        ),
    ],
)
def test_runner_vitest_rejects_unsafe_shapes(worker_workspace, tmp_path, argv, expected):
    test_file = worker_workspace / "src/components/kitchen/WeekMobileList.test.tsx"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("import { describe, it } from 'vitest'\n", encoding="utf-8")
    non_test_file = worker_workspace / "src/components/kitchen/WeekMobileList.tsx"
    non_test_file.write_text("export default function WeekMobileList() {}\n", encoding="utf-8")
    (tmp_path / "outside.test.ts").write_text("import { it } from 'vitest'\n", encoding="utf-8")

    result = _run({"argv": argv})

    assert "error" in result
    assert expected in result["error"]


def test_runner_ignores_untrusted_path_pytest(worker_workspace, monkeypatch):
    (worker_workspace / "test_probe.py").write_text(
        "def test_probe():\n    assert True\n",
        encoding="utf-8",
    )
    hijack = worker_workspace / "pytest"
    hijack.write_text("#!/bin/sh\necho pytest-path-hijack\n", encoding="utf-8")
    hijack.chmod(hijack.stat().st_mode | 0o111)
    monkeypatch.setenv("PATH", str(worker_workspace))

    result = _run({"argv": ["pytest", "test_probe.py"], "timeout_seconds": 20})

    assert result["ok"] is True
    assert "pytest-path-hijack" not in result["stdout"]
    assert "1 passed" in result["stdout"]
