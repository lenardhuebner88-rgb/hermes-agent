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


def test_python_mappers_share_a_cap_and_the_shell_keeps_its_lower_bound():
    """The two PYTHON mappers must agree; the shell wrapper must NOT join them.

    This is a two-layer design, not drift:
      - both Python mappers (worker-side selection and the post-merge
        integration gate) share _FALLBACK_MAX_TEST_FILES = 800;
      - scripts/affected-tests.sh applies its OWN, lower bound (200) because it
        is the interactive worker gate and trades the broad package fallback
        for tempo, announcing the omission on stderr.

    Coupling the shell to the Python constant (2026-07-25) erased that tempo
    bound and broke test_run_affected_mapping.py. Unifying the Python side DOWN
    to 200 instead was measured to drop 45 source files to zero merge-gate
    selection. Both directions are regressions; the layers stay separate.
    """
    standalone = _load_standalone_mapper()
    shell = (REPO_ROOT / "scripts" / "affected-tests.sh").read_text(encoding="utf-8")

    assert standalone._FALLBACK_MAX_TEST_FILES == 800
    assert kanban_worktrees._FALLBACK_MAX_TEST_FILES == 800
    # The shell keeps its own, deliberately lower literal.
    assert "FALLBACK_MAX_TEST_FILES=200" in shell
    assert "--fallback-max-test-files" not in shell
