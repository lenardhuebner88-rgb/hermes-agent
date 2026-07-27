"""Unit tests for scripts/affected_tests.py — the targeted-test-scope helper.

Covers the diff -> pytest-module mapping (the one piece of real logic). The
mapping mirrors hermes_cli.kanban_worktrees._affected_pytest_modules; this also
guards against the two drifting apart.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_UNMAPPED_TRACKED_PYTHON_SOURCES = 349


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


def test_monolith_source_falls_back_to_package_dir():
    """When a source file has no 1:1 test_<name>.py (e.g. gateway/run.py),
    the entire tests/<pkg>/ directory is selected so feature-named tests
    still run at the merge gate."""
    mod = _load_module()
    # gateway/run.py has no tests/gateway/test_run.py but tests/gateway/ exists.
    out = mod.affected_pytest_modules(REPO_ROOT, ["gateway/run.py"])
    assert "tests/gateway/" in out


def test_known_hermes_cli_monoliths_use_explicit_test_mappings():
    """Known monoliths select their maintained feature tests, not the package.

    Reads the mapping from the module instead of restating it. A duplicated
    fixture list means every legitimate addition to the table fails this test for
    no reason, which is how the two copies drift apart. The assertions that
    actually catch bugs are kept: every configured pattern must still match a
    real test file (so a rename or typo is caught rather than silently selecting
    nothing), and the selection must not degrade to the whole package directory.
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

        assert selected == expected
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


def test_oversize_package_dir_downgrades_to_no_selection(tmp_path):
    """When the package test directory exceeds _FALLBACK_MAX_TEST_FILES,
    the fallback downgrades to no selection — the nightly full suite
    remains the backstop (AC-2 counter-metric)."""
    mod = _load_module()
    # Build a fake repo: gateway/run.py with no 1:1 test, but a bloated
    # tests/gateway/ directory that exceeds the cap.
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "run.py").write_text("x = 1\n")
    pkg = tmp_path / "tests" / "gateway"
    pkg.mkdir(parents=True)
    cap = mod._FALLBACK_MAX_TEST_FILES
    for i in range(cap + 1):
        (pkg / f"test_{i:04d}.py").write_text("def t(): pass\n")
    out = mod.affected_pytest_modules(tmp_path, ["gateway/run.py"])
    assert "tests/gateway/" not in out
    assert out == []


def test_fallback_does_not_fire_for_root_source():
    """A root-level source file (no package dir) must not select tests/
    itself — that would be the full suite."""
    mod = _load_module()
    out = mod.affected_pytest_modules(REPO_ROOT, ["run_agent.py"])
    # run_agent.py -> tests/test_run_agent.py; if absent, rel_dir is "." so
    # pkg_test_dir == tests/ which is explicitly excluded.
    assert "tests/" not in out


def test_matches_kanban_worktrees_mapping():
    """The standalone copy must agree with the gate's implementation."""
    mod = _load_module()
    from hermes_cli.kanban_worktrees import _affected_pytest_modules

    sample = [
        "hermes_cli/config.py",
        "gateway/run.py",
        "tests/hermes_cli/test_kanban_cli.py",
        "README.md",
        "tests/stress/test_x.py",
    ]
    assert mod.affected_pytest_modules(REPO_ROOT, sample) == _affected_pytest_modules(
        REPO_ROOT, sample
    )


def test_changed_module_selects_feature_named_sibling_tests_from_imports():
    """The explicit kanban DB mapping retains its feature-split DB tests.

    tests/hermes_cli/test_kanban_db.py was split into domain files
    (2213f85be), so the mapping must retain those files."""
    mod = _load_module()

    selected = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/kanban_db.py"])

    assert "tests/hermes_cli/test_kanban_db_schema.py" in selected
    assert "tests/test_design_board_kanban.py" not in selected


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


def test_fallback_cap_covers_hermes_cli_package_dir():
    """tests/hermes_cli/ (592 files at calibration) is under the raised cap,
    so a hermes_cli source without a 1:1 test file selects the package
    directory again instead of silently downgrading to no selection."""
    mod = _load_module()
    # tests/hermes_cli/test_design_board_store.py does not exist (the 1:1
    # test lives at tests/ root), so the directory fallback applies.
    out = mod.affected_pytest_modules(REPO_ROOT, ["hermes_cli/design_board_store.py"])
    assert "tests/hermes_cli/" in out


def test_unmapped_python_source_count_does_not_regress():
    """Ratchet the measured false-green surface without requiring it to be zero.

    The 2026-07-25 nested-package fix reduced the repository-wide count from
    417 to 349.  Include untracked worktree files so this guard also catches a
    new source file before it is committed.
    """
    mod = _load_module()
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    sources = sorted(
        path
        for path in proc.stdout.splitlines()
        if path
        and not path.startswith("tests/")
        and (REPO_ROOT / path).is_file()
    )
    imports_by_test_dir: dict[Path, set[str]] = {}
    for test_path in (REPO_ROOT / "tests").rglob("test_*.py"):
        imported: set[str] = set()
        try:
            lines = test_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            direct = re.match(r"^\s*import\s+(?P<names>.*)$", line)
            if direct:
                imported.update(
                    part.strip().split()[0]
                    for part in direct.group("names").split(",")
                    if part.strip()
                )
            from_import = re.match(
                r"^\s*from\s+(?P<module>[A-Za-z_][\w.]*)\s+import\b"
                r"(?P<names>.*)$",
                line,
            )
            if from_import:
                package = from_import.group("module")
                imported.add(package)
                imported.update(
                    f"{package}.{name}"
                    for name in re.findall(
                        r"\b[A-Za-z_]\w*\b",
                        from_import.group("names"),
                    )
                    if name not in {"as", "import"}
                )
        imports_by_test_dir.setdefault(test_path.parent, set()).update(imported)

    def maps_to_test(source_path: str) -> bool:
        source = Path(source_path)
        rel_dir = str(source.parent)
        candidate = Path("tests") / rel_dir / f"test_{source.name}"
        if (REPO_ROOT / candidate).is_file():
            return True
        if mod._mapped_monolith_tests(REPO_ROOT, source_path):
            return True
        module_import = str(source.with_suffix("")).replace("/", ".")
        test_dirs = [REPO_ROOT / "tests"]
        package_test_dir = Path("tests") / rel_dir
        while package_test_dir != Path("tests"):
            absolute = REPO_ROOT / package_test_dir
            if absolute.is_dir():
                test_dirs.append(absolute)
            package_test_dir = package_test_dir.parent
        if any(
            module_import in imports_by_test_dir.get(test_dir, set())
            for test_dir in test_dirs
        ):
            return True
        package_test_dir = REPO_ROOT / "tests" / rel_dir
        return (
            package_test_dir != REPO_ROOT / "tests"
            and package_test_dir.is_dir()
            and sum(1 for _path in package_test_dir.glob("test_*.py"))
            <= mod._FALLBACK_MAX_TEST_FILES
        )

    unmapped = [source for source in sources if not maps_to_test(source)]

    assert len(unmapped) <= _MAX_UNMAPPED_TRACKED_PYTHON_SOURCES, (
        f"{len(unmapped)} Python source files select no pytest module "
        f"(ratchet {_MAX_UNMAPPED_TRACKED_PYTHON_SOURCES}); "
        f"first new blind spots: {unmapped[:20]}"
    )
