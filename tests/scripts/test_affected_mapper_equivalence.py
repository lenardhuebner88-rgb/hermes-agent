"""Behavioral contracts for the affected-test compatibility wrappers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest

from hermes_cli import kanban_worktrees
from hermes_cli.affected_test_mapping import (
    INTEGRATION_FALLBACK_MAX_TEST_FILES,
    MappingError,
    WORKER_FALLBACK_MAX_TEST_FILES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_standalone_mapper():
    spec = importlib.util.spec_from_file_location(
        "affected_tests_equivalence",
        REPO_ROOT / "scripts" / "affected_tests.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_auth_middleware_selects_real_dedicated_tests_for_both_mappers():
    expected = [
        "tests/hermes_cli/test_dashboard_auth_401_reauth.py",
        "tests/hermes_cli/test_dashboard_auth_audit.py",
        "tests/hermes_cli/test_dashboard_auth_cookies.py",
        "tests/hermes_cli/test_dashboard_auth_gate.py",
        "tests/hermes_cli/test_dashboard_auth_middleware.py",
        "tests/hermes_cli/test_dashboard_auth_native_flow.py",
        "tests/hermes_cli/test_dashboard_auth_password_login.py",
        "tests/hermes_cli/test_dashboard_auth_plugin_hook.py",
        "tests/hermes_cli/test_dashboard_auth_prefix.py",
        "tests/hermes_cli/test_dashboard_auth_provider_base.py",
        "tests/hermes_cli/test_dashboard_auth_status_endpoint.py",
        "tests/hermes_cli/test_dashboard_auth_stub_provider.py",
        "tests/hermes_cli/test_dashboard_auth_ws_auth.py",
        "tests/hermes_cli/test_dashboard_auth_ws_tickets.py",
        "tests/hermes_cli/test_web_server.py",
        "tests/plugins/test_plugin_dashboard_auth_contract.py",
    ]
    changed = ["hermes_cli/dashboard_auth/middleware.py"]
    standalone = _load_standalone_mapper()

    assert standalone.affected_pytest_modules(REPO_ROOT, changed) == expected
    assert kanban_worktrees._affected_pytest_modules(REPO_ROOT, changed) == expected


@pytest.mark.parametrize("wrapper", ["standalone", "integration"])
def test_public_wrappers_raise_instead_of_discarding_unmapped(
    tmp_path: Path,
    wrapper: str,
) -> None:
    (tmp_path / "orphan.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    standalone = _load_standalone_mapper()
    call = (
        standalone.affected_pytest_modules
        if wrapper == "standalone"
        else kanban_worktrees._affected_pytest_modules
    )

    with pytest.raises(MappingError, match="unmapped production paths: orphan.py"):
        call(tmp_path, ["orphan.py"])


def test_compatibility_wrappers_pin_integration_mode(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "runtime.py").write_text("VALUE = 1\n")
    tests = tmp_path / "tests" / "pkg"
    tests.mkdir(parents=True)
    for index in range(WORKER_FALLBACK_MAX_TEST_FILES + 1):
        (tests / f"test_{index:03d}.py").write_text(
            "def test_placeholder():\n    assert True\n"
        )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    standalone = _load_standalone_mapper()

    assert standalone.affected_pytest_modules(
        tmp_path,
        ["pkg/runtime.py"],
    ) == ["tests/pkg/"]
    assert kanban_worktrees._affected_pytest_modules(
        tmp_path,
        ["pkg/runtime.py"],
    ) == ["tests/pkg/"]


def test_worker_cap_runs_end_to_end_through_shell_wrapper(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "affected-tests.sh", scripts)
    shutil.copy2(REPO_ROOT / "scripts" / "affected_tests.py", scripts)
    (repo / "hermes_cli").mkdir()
    (repo / "hermes_cli" / "__init__.py").write_text("")
    shutil.copy2(
        REPO_ROOT / "hermes_cli" / "affected_test_mapping.py",
        repo / "hermes_cli",
    )
    (repo / "config").mkdir()
    (repo / "config" / "affected-test-exceptions.json").write_text(
        '{"schema_version": 1, "exceptions": []}\n'
    )
    (repo / "pkg").mkdir()
    source = repo / "pkg" / "runtime.py"
    source.write_text("VALUE = 1\n")
    tests = repo / "tests" / "pkg"
    tests.mkdir(parents=True)
    for index in range(WORKER_FALLBACK_MAX_TEST_FILES + 1):
        (tests / f"test_{index:03d}.py").write_text(
            "def test_placeholder():\n    assert True\n"
        )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "baseline",
        ],
        cwd=repo,
        check=True,
    )
    source.write_text("VALUE = 2\n")

    proc = subprocess.run(
        [str(scripts / "affected-tests.sh"), "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 4
    assert proc.stdout.strip() == ""
    assert "mode limit is 200" in proc.stderr


def test_shared_classifier_owns_both_caps():
    standalone = _load_standalone_mapper()

    assert WORKER_FALLBACK_MAX_TEST_FILES == 200
    assert INTEGRATION_FALLBACK_MAX_TEST_FILES == 800
    assert standalone._FALLBACK_MAX_TEST_FILES == INTEGRATION_FALLBACK_MAX_TEST_FILES
    assert (
        kanban_worktrees._FALLBACK_MAX_TEST_FILES
        == INTEGRATION_FALLBACK_MAX_TEST_FILES
    )
