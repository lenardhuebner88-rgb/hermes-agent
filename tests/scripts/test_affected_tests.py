"""Unit tests for scripts/affected_tests.py — the targeted-test-scope helper."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

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
    # hermes_cli/commands.py -> tests/hermes_cli/test_commands.py (both real).
    out = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/commands.py"])
    assert "tests/hermes_cli/test_commands.py" in out


def test_changed_test_file_runs_itself():
    mod = _load_module()
    out = mod.affected_pytest_modules(
        REPO_ROOT, ["tests/hermes_cli/test_commands.py"]
    )
    assert out == ["tests/hermes_cli/test_commands.py"]


def test_non_python_paths_yield_nothing():
    mod = _load_module()
    out = mod.affected_pytest_modules(
        REPO_ROOT,
        ["README.md", "web/src/control/views/CommandHome.tsx"],
    )
    assert out == []


def test_gate_control_script_selects_its_contract_tests():
    mod = _load_module()

    out = mod.affected_pytest_modules(REPO_ROOT, ["scripts/affected-tests.sh"])

    assert "tests/scripts/test_affected_mapper_equivalence.py" in out
    assert "tests/scripts/test_run_affected.py" in out


def test_stress_scripts_are_skipped():
    mod = _load_module()
    out = mod.affected_pytest_modules(REPO_ROOT, ["tests/stress/test_anything.py"])
    assert out == []


def test_monolith_source_selects_focused_import_tests():
    mod = _load_module()

    out = mod.affected_pytest_modules(REPO_ROOT, ["gateway/run.py"])

    assert out
    assert "tests/gateway/" not in out


def test_known_hermes_cli_monoliths_include_explicit_test_mappings():
    """Known monoliths retain every maintained feature test without a package fallback.

    Reads the mapping from the module instead of restating it. A duplicated
    fixture list means every legitimate addition to the table fails this test for
    no reason, which is how the two copies drift apart. The assertions that
    actually catch bugs are kept: every configured pattern must still match a
    real test file (so a rename or typo is caught rather than silently selecting
    nothing), every explicit target remains selected, and the selection must not
    degrade to the whole package directory. Direct and import-index tests are
    deliberately additive.
    """
    mod = _load_module()

    assert mod._MONOLITH_TEST_PATTERNS, "the monolith table is empty"

    for source, patterns in mod._MONOLITH_TEST_PATTERNS.items():
        assert (REPO_ROOT / source).is_file(), f"mapped source no longer exists: {source}"
        for pattern in patterns:
            assert list(REPO_ROOT.glob(pattern)), (
                f"pattern for {source} matches no test file: {pattern}"
            )

        expected = sorted(
            {
                str(path.relative_to(REPO_ROOT))
                for pattern in patterns
                for path in REPO_ROOT.glob(pattern)
                if path.is_file()
            }
        )
        selected = mod.affected_pytest_modules(REPO_ROOT, [source])

        assert set(expected) <= set(selected)
        # No package-wide fallback: that would turn the targeted gate into a
        # de-facto full-suite run (AC-2 counter-metric).
        assert not any(s.endswith("/") for s in selected), selected


def test_kanban_worktrees_selects_lifecycle_anchor_checker():
    """Both lifecycle-map source files must select the anchor checker."""
    mod = _load_module()

    selected = mod.affected_pytest_modules(
        REPO_ROOT, ["hermes_cli/kanban_worktrees.py"]
    )

    assert "tests/scripts/test_check_kanban_lifecycle_anchors.py" in selected


def test_oversize_package_dir_is_unmapped(tmp_path):
    mod = _load_module()
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "run.py").write_text("x = 1\n")
    pkg = tmp_path / "tests" / "gateway"
    pkg.mkdir(parents=True)
    cap = mod._FALLBACK_MAX_TEST_FILES
    for i in range(cap + 1):
        (pkg / f"test_{i:04d}.py").write_text("def t(): pass\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    plan = mod.classify_changed_paths(
        tmp_path,
        ["gateway/run.py"],
        mode="integration",
    )

    assert plan.selected_tests == []
    assert plan.unmapped_paths == ["gateway/run.py"]
    assert "mode limit is 800" in plan.records[0].warnings[0]


def test_fallback_does_not_fire_for_root_source():
    """A root-level source file (no package dir) must not select tests/
    itself — that would be the full suite."""
    mod = _load_module()
    out = mod.affected_pytest_modules(REPO_ROOT, ["run_agent.py"])
    # run_agent.py -> tests/test_run_agent.py; if absent, rel_dir is "." so
    # pkg_test_dir == tests/ which is explicitly excluded.
    assert "tests/" not in out


def test_changed_module_selects_feature_named_sibling_tests_from_imports():
    """The explicit kanban DB mapping unions feature-split and import tests.

    tests/hermes_cli/test_kanban_db.py was split into domain files
    (2213f85be), so the mapping must retain those files without discarding
    other tests that import the production module."""
    mod = _load_module()

    selected = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/kanban_db.py"])

    assert "tests/hermes_cli/test_kanban_db_schema.py" in selected
    assert "tests/test_design_board_kanban.py" in selected


def test_changed_module_selects_submodule_from_import_sibling_tests():
    """Feature siblings often use ``from pkg.module import Symbol`` imports."""
    mod = _load_module()

    selected = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/commands.py"])

    assert "tests/hermes_cli/test_commands.py" in selected
    assert "tests/hermes_cli/test_goals.py" in selected
    assert "tests/hermes_cli/" not in selected


def test_changed_module_selects_root_level_sibling_tests():
    """Feature tests also live directly at tests/ root: changing
    hermes_cli/design_board_store.py must select
    tests/test_design_board_store.py (zero-selection blind spot)."""
    mod = _load_module()

    selected = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/design_board_store.py"])

    assert "tests/test_design_board_store.py" in selected


def test_feature_mapping_avoids_hermes_cli_package_fallback():
    mod = _load_module()

    out = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/design_board_store.py"])

    assert "tests/test_design_board_store.py" in out
    assert "tests/hermes_cli/" not in out


def test_repository_inventory_has_zero_unmapped_production_sources():
    mod = _load_module()

    worker = mod.census_repository(REPO_ROOT, mode="worker")
    integration = mod.census_repository(REPO_ROOT, mode="integration")

    assert worker.unmapped_paths == []
    assert integration.unmapped_paths == []
