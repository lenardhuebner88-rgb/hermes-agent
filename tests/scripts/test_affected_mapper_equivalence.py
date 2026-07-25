"""Acceptance coverage for the two affected-pytest mappers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hermes_cli import kanban_worktrees

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


def _selections(changed_files: list[str]) -> tuple[list[str], list[str]]:
    standalone = _load_standalone_mapper()
    return (
        standalone.affected_pytest_modules(REPO_ROOT, changed_files),
        kanban_worktrees._affected_pytest_modules(REPO_ROOT, changed_files),
    )


def test_dashboard_auth_middleware_selects_real_dedicated_tests_for_both_mappers():
    expected = [
        "tests/hermes_cli/test_dashboard_auth_401_reauth.py",
        "tests/hermes_cli/test_dashboard_auth_middleware.py",
        "tests/hermes_cli/test_web_server.py",
    ]

    standalone, integration = _selections(
        ["hermes_cli/dashboard_auth/middleware.py"]
    )

    assert standalone == expected
    assert integration == expected


@pytest.mark.parametrize(
    "changed_files",
    [
        ["hermes_cli/dashboard_auth/middleware.py"],
        ["hermes_cli/kanban_db.py"],
        ["tests/hermes_cli/test_dashboard_auth_middleware.py"],
    ],
    ids=["nested-package", "kanban-db", "changed-test"],
)
def test_both_mappers_agree_for_required_real_inputs(changed_files):
    standalone, integration = _selections(changed_files)

    assert standalone == integration


def test_fallback_cap_agrees_in_both_python_mappers_and_shell_has_no_literal():
    """The two gates must share ONE cap, and it must be the higher one.

    The gates used to disagree (shell 200, post-merge 800). Unifying DOWN was
    measured to drop 45 source files to zero merge-gate selection, because
    tests/agent/, tests/gateway/ and tests/hermes_cli/ all exceed 200 — the
    'silently downgrading to no selection' failure the cap tests in
    test_affected_tests.py already guard against. So they unify UP.
    """
    standalone = _load_standalone_mapper()
    shell = (REPO_ROOT / "scripts" / "affected-tests.sh").read_text(encoding="utf-8")

    assert standalone._FALLBACK_MAX_TEST_FILES == 800
    assert kanban_worktrees._FALLBACK_MAX_TEST_FILES == 800
    # The shell must READ the Python constant, never carry its own literal.
    assert "--fallback-max-test-files" in shell
    assert "FALLBACK_MAX_TEST_FILES=200" not in shell
    assert "FALLBACK_MAX_TEST_FILES=800" not in shell
