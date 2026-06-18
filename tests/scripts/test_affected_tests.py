"""Unit tests for scripts/affected_tests.py — the targeted-test-scope helper.

Covers the diff -> pytest-module mapping (the one piece of real logic). The
mapping mirrors hermes_cli.kanban_worktrees._affected_pytest_modules; this also
guards against the two drifting apart.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "affected_tests", REPO_ROOT / "scripts" / "affected_tests.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_source_file_maps_to_its_test_file():
    mod = _load_module()
    # hermes_cli/kanban_db.py -> tests/hermes_cli/test_kanban_db.py (both real).
    out = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/kanban_db.py"])
    assert "tests/hermes_cli/test_kanban_db.py" in out


def test_changed_test_file_runs_itself():
    mod = _load_module()
    out = mod.affected_pytest_modules(
        REPO_ROOT, ["tests/hermes_cli/test_kanban_db.py"]
    )
    assert out == ["tests/hermes_cli/test_kanban_db.py"]


def test_non_python_and_unmapped_yield_nothing():
    mod = _load_module()
    out = mod.affected_pytest_modules(
        REPO_ROOT,
        ["README.md", "web/src/control/views/CommandHome.tsx", "scripts/affected-tests.sh"],
    )
    assert out == []


def test_stress_scripts_are_skipped():
    mod = _load_module()
    out = mod.affected_pytest_modules(REPO_ROOT, ["tests/stress/test_anything.py"])
    assert out == []


def test_matches_kanban_worktrees_mapping():
    """The standalone copy must agree with the gate's implementation."""
    mod = _load_module()
    from hermes_cli.kanban_worktrees import _affected_pytest_modules

    sample = [
        "hermes_cli/kanban_db.py",
        "hermes_cli/config.py",
        "tests/hermes_cli/test_kanban_cli.py",
        "README.md",
        "tests/stress/test_x.py",
    ]
    assert mod.affected_pytest_modules(REPO_ROOT, sample) == _affected_pytest_modules(
        REPO_ROOT, sample
    )
