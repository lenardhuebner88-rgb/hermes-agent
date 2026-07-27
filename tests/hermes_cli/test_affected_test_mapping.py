from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from hermes_cli.affected_test_mapping import (
    EXPLICIT_TEST_PATTERNS,
    INTEGRATION_FALLBACK_MAX_TEST_FILES,
    MappingError,
    UNMAPPED_EXIT_CODE,
    WORKER_FALLBACK_MAX_TEST_FILES,
    affected_pytest_modules,
    build_test_index,
    changed_paths,
    census_repository,
    classify_changed_paths,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")


def _write_exceptions(repo: Path, entries: list[dict[str, str]]) -> Path:
    path = repo / "affected-exceptions.json"
    path.write_text(
        json.dumps({"schema_version": 1, "exceptions": entries}),
        encoding="utf-8",
    )
    return path


def _exception(path: str, **overrides: str) -> dict[str, str]:
    entry = {
        "path": path,
        "disposition": "manual_only",
        "reason": "platform-only contract",
        "owner": "runtime",
        "area": "test fixture",
        "review_by": "2099-12-31",
    }
    entry.update(overrides)
    return entry


def test_classifies_selected_not_applicable_allowlisted_and_unmapped(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "covered.py").write_text("VALUE = 1\n")
    (tmp_path / "allowed.py").write_text("VALUE = 1\n")
    (tmp_path / "unmapped.py").write_text("VALUE = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "catalog.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_covered.py").write_text(
        "def test_covered():\n    assert True\n"
    )
    exceptions = _write_exceptions(
        tmp_path,
        [_exception("allowed.py")],
    )
    _init_repo(tmp_path)

    plan = classify_changed_paths(
        tmp_path,
        [
            "pkg/covered.py",
            "allowed.py",
            "unmapped.py",
            "docs/catalog.py",
            "README.md",
        ],
        mode="worker",
        exceptions_path=exceptions,
    )

    assert {record.path: record.state for record in plan.records} == {
        "README.md": "not_applicable",
        "allowed.py": "allowlisted",
        "docs/catalog.py": "not_applicable",
        "pkg/covered.py": "selected",
        "unmapped.py": "unmapped",
    }
    assert plan.selected_tests == ["tests/pkg/test_covered.py"]
    assert plan.unmapped_paths == ["unmapped.py"]


def test_worker_and_integration_caps_are_part_of_classification(
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
    _init_repo(tmp_path)

    worker = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="worker",
    )
    integration = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="integration",
    )

    assert WORKER_FALLBACK_MAX_TEST_FILES == 200
    assert INTEGRATION_FALLBACK_MAX_TEST_FILES == 800
    assert worker.unmapped_paths == ["pkg/runtime.py"]
    assert worker.selected_tests == []
    assert integration.unmapped_paths == []
    assert integration.selected_tests == ["tests/pkg/"]


def test_exception_before_package_fallback_is_mode_independent(
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
    exceptions = _write_exceptions(
        tmp_path,
        [_exception("pkg/runtime.py")],
    )
    _init_repo(tmp_path)

    worker = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="worker",
        exceptions_path=exceptions,
    )
    integration = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="integration",
        exceptions_path=exceptions,
    )

    assert worker.records[0].state == "allowlisted"
    assert integration.records[0].state == "allowlisted"
    assert worker.selected_tests == []
    assert integration.selected_tests == []


def test_mixed_selected_and_unmapped_input_remains_unmapped(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "covered.py").write_text("VALUE = 1\n")
    (tmp_path / "orphan.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_covered.py").write_text(
        "def test_covered():\n    assert True\n"
    )
    _init_repo(tmp_path)

    plan = classify_changed_paths(
        tmp_path,
        ["pkg/covered.py", "orphan.py"],
        mode="worker",
    )

    assert plan.selected_tests == ["tests/pkg/test_covered.py"]
    assert plan.unmapped_paths == ["orphan.py"]


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([_exception("pkg/*.py")], "must be exact"),
        (
            [_exception("pkg/runtime.py"), _exception("pkg/runtime.py")],
            "duplicate",
        ),
    ],
)
def test_invalid_exception_inventory_fails_closed(
    tmp_path: Path,
    entries: list[dict[str, str]],
    message: str,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "runtime.py").write_text("VALUE = 1\n")
    exceptions = _write_exceptions(tmp_path, entries)
    _init_repo(tmp_path)

    with pytest.raises(MappingError, match=message):
        classify_changed_paths(
            tmp_path,
            ["pkg/runtime.py"],
            mode="worker",
            exceptions_path=exceptions,
        )


def test_expired_exception_degrades_only_its_path_to_unmapped(
    tmp_path: Path,
) -> None:
    (tmp_path / "expired.py").write_text("VALUE = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "covered.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_covered.py").write_text(
        "def test_covered():\n    assert True\n"
    )
    exceptions = _write_exceptions(
        tmp_path,
        [_exception("expired.py", review_by="2020-01-01")],
    )
    _init_repo(tmp_path)

    plan = classify_changed_paths(
        tmp_path,
        ["expired.py", "pkg/covered.py"],
        mode="worker",
        exceptions_path=exceptions,
    )

    assert plan.unmapped_paths == ["expired.py"]
    assert plan.selected_tests == ["tests/pkg/test_covered.py"]
    assert "expired affected-test exception ignored" in plan.records[0].warnings[0]


def test_stale_exception_degrades_its_path_without_breaking_other_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "covered.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_covered.py").write_text(
        "def test_covered():\n    assert True\n"
    )
    exceptions = _write_exceptions(
        tmp_path,
        [_exception("removed.py")],
    )
    _init_repo(tmp_path)

    plan = classify_changed_paths(
        tmp_path,
        ["removed.py", "pkg/covered.py"],
        mode="worker",
        exceptions_path=exceptions,
    )

    assert plan.unmapped_paths == ["removed.py"]
    assert plan.selected_tests == ["tests/pkg/test_covered.py"]
    assert "stale affected-test exception ignored" in plan.records[1].warnings[0]


def test_mapped_path_cannot_remain_in_exception_inventory(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "runtime.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_runtime.py").write_text(
        "def test_runtime():\n    assert True\n"
    )
    exceptions = _write_exceptions(
        tmp_path,
        [_exception("pkg/runtime.py")],
    )
    _init_repo(tmp_path)

    with pytest.raises(MappingError, match="must not remain allowlisted"):
        classify_changed_paths(
            tmp_path,
            ["pkg/runtime.py"],
            mode="worker",
            exceptions_path=exceptions,
        )


def test_changed_paths_excludes_untracked_files_with_explicit_ref(
    tmp_path: Path,
) -> None:
    (tmp_path / "tracked.py").write_text("VALUE = 1\n")
    _init_repo(tmp_path)
    (tmp_path / "new.py").write_text("VALUE = 2\n")

    assert changed_paths(tmp_path, "HEAD") == []


def test_deleted_production_without_surviving_test_is_not_applicable(
    tmp_path: Path,
) -> None:
    (tmp_path / "obsolete.py").write_text("VALUE = 1\n")
    _init_repo(tmp_path)
    (tmp_path / "obsolete.py").unlink()

    changed = changed_paths(tmp_path, "HEAD")
    plan = classify_changed_paths(tmp_path, changed, mode="worker")

    assert changed == ["obsolete.py"]
    assert plan.unmapped_paths == []
    assert plan.records[0].state == "not_applicable"
    assert plan.records[0].scope == "deleted_production"


def test_deleted_production_selects_surviving_importer(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "foo.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_surviving_importer.py").write_text(
        "from pkg import foo\n\n"
        "def test_import():\n"
        "    assert foo.VALUE == 1\n"
    )
    _init_repo(tmp_path)
    (tmp_path / "pkg" / "foo.py").unlink()

    changed = changed_paths(tmp_path, "HEAD")
    record = classify_changed_paths(
        tmp_path,
        changed,
        mode="worker",
    ).records[0]

    assert changed == ["pkg/foo.py"]
    assert record.state == "selected"
    assert record.strategies == ("import",)
    assert record.tests == ("tests/test_surviving_importer.py",)


def test_changed_paths_includes_typechange_without_treating_it_as_deletion(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "runtime.py").write_text("VALUE = 1\n")
    (package / "target.py").write_text("VALUE = 2\n")
    tests = tmp_path / "tests" / "pkg"
    tests.mkdir(parents=True)
    (tests / "test_runtime.py").write_text(
        "def test_runtime():\n"
        "    assert True\n"
    )
    _init_repo(tmp_path)

    (package / "runtime.py").unlink()
    (package / "runtime.py").symlink_to("target.py")

    changed = changed_paths(tmp_path, "HEAD")
    record = classify_changed_paths(
        tmp_path,
        changed,
        mode="worker",
    ).records[0]

    assert " T\tpkg/runtime.py" in _git(tmp_path, "diff", "--raw", "HEAD").stdout
    assert changed == ["pkg/runtime.py"]
    assert record.state == "selected"
    assert record.scope == "production_python"
    assert record.strategies == ("direct",)
    assert record.tests == ("tests/pkg/test_runtime.py",)


def test_changed_paths_without_main_falls_back_to_head(tmp_path: Path) -> None:
    (tmp_path / "tracked.py").write_text("VALUE = 1\n")
    _git(tmp_path, "init", "-q", "-b", "master")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    (tmp_path / "tracked.py").write_text("VALUE = 2\n")

    assert changed_paths(tmp_path, None) == ["tracked.py"]


def test_invalid_git_ref_is_a_mapping_error() -> None:
    proc = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / "scripts" / "affected_tests.py"),
            "--mode",
            "worker",
            "definitely-not-a-ref",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 2
    assert "mapping error" in proc.stderr


def test_all_explicit_patterns_match_real_tests() -> None:
    for source, patterns in EXPLICIT_TEST_PATTERNS.items():
        assert (REPO_ROOT / source).is_file(), source
        for pattern in patterns:
            assert list(REPO_ROOT.glob(pattern)), (source, pattern)


def test_explicit_direct_and_import_strategies_are_unioned(tmp_path: Path) -> None:
    source = tmp_path / "hermes_cli" / "kanban_db.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    tests = tmp_path / "tests" / "hermes_cli"
    tests.mkdir(parents=True)
    (tests / "test_kanban_db.py").write_text("def test_direct():\n    assert True\n")
    (tests / "test_kanban_db_explicit.py").write_text(
        "def test_explicit():\n    assert True\n"
    )
    (tests / "test_importer.py").write_text(
        "from hermes_cli import kanban_db\n\n"
        "def test_import():\n    assert kanban_db.VALUE\n"
    )
    _init_repo(tmp_path)

    record = classify_changed_paths(
        tmp_path,
        ["hermes_cli/kanban_db.py"],
        mode="worker",
    ).records[0]

    assert record.strategies == ("explicit", "direct", "import")
    assert set(record.tests) == {
        "tests/hermes_cli/test_importer.py",
        "tests/hermes_cli/test_kanban_db.py",
        "tests/hermes_cli/test_kanban_db_explicit.py",
    }


def test_real_package_init_import_scope_is_not_package_fallback_capped() -> None:
    record = classify_changed_paths(
        REPO_ROOT,
        ["hermes_cli/__init__.py"],
        mode="worker",
    ).records[0]

    assert record.state == "selected"
    assert record.strategies == ("explicit", "import")
    assert len(record.tests) > WORKER_FALLBACK_MAX_TEST_FILES
    assert "package_fallback" not in record.strategies


def test_stress_registry_files_are_not_pytest_import_evidence() -> None:
    index = build_test_index(REPO_ROOT)
    record = classify_changed_paths(
        REPO_ROOT,
        ["hermes_cli/kanban_db.py"],
        mode="worker",
        index=index,
    ).records[0]

    assert "tests/stress/test_atypical_scenarios.py" not in index.paths
    assert "tests/stress/test_kanban_worktree_concurrency.py" not in index.paths
    assert "tests/stress/test_atypical_scenarios.py" not in record.tests
    assert "tests/stress/test_kanban_worktree_concurrency.py" not in record.tests


@pytest.mark.parametrize("mode", ["worker", "integration"])
@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "tests/hermes_cli/conftest.py",
        "tests/fakes/__init__.py",
        "tests/hermes_cli/__init__.py",
    ],
)
def test_python_test_support_is_documented_not_applicable(
    path: str,
    mode: str,
) -> None:
    record = classify_changed_paths(
        REPO_ROOT,
        [path],
        mode=mode,
    ).records[0]

    assert (REPO_ROOT / path).is_file()
    assert record.state == "not_applicable"
    assert record.scope == "test_support"
    assert record.strategies == ()
    assert record.tests == ()


def test_real_fixture_string_import_is_not_indexed() -> None:
    index = build_test_index(REPO_ROOT)

    assert "tests/hermes_cli/test_pa_graph.py" not in index.imports.get(
        "hermes_cli.web_server",
        (),
    )


def test_invalid_import_inside_string_cannot_crash_or_supply_coverage(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "runtime.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture.py").write_text(
        'CODE = """from pkg.runtime import ()"""\n'
    )
    _init_repo(tmp_path)

    plan = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="worker",
    )

    assert plan.unmapped_paths == ["pkg/runtime.py"]
    assert plan.selected_tests == []


def test_affected_pytest_modules_raises_on_unmapped(tmp_path: Path) -> None:
    (tmp_path / "orphan.py").write_text("VALUE = 1\n")
    _init_repo(tmp_path)

    with pytest.raises(MappingError, match="unmapped production paths: orphan.py"):
        affected_pytest_modules(tmp_path, ["orphan.py"], mode="worker")


def test_census_ignores_tracked_file_deleted_from_worktree(tmp_path: Path) -> None:
    (tmp_path / "obsolete.py").write_text("VALUE = 1\n")
    _init_repo(tmp_path)
    (tmp_path / "obsolete.py").unlink()

    plan = census_repository(tmp_path, mode="worker")

    assert plan.records == ()
    assert plan.unmapped_paths == []


def test_census_ignores_untracked_production_work_in_progress(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "covered.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_covered.py").write_text(
        "def test_covered():\n"
        "    assert True\n"
    )
    _init_repo(tmp_path)
    (tmp_path / "untracked_slice.py").write_text("VALUE = 2\n")

    plan = census_repository(tmp_path, mode="worker")

    assert {record.path for record in plan.records} == {"pkg/covered.py"}
    assert plan.unmapped_paths == []


def test_synthetic_production_path_is_unmapped(tmp_path: Path) -> None:
    source = tmp_path / "synthetic" / "unmapped_contract.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    _init_repo(tmp_path)

    plan = classify_changed_paths(
        tmp_path,
        ["synthetic/unmapped_contract.py"],
        mode="worker",
    )

    assert UNMAPPED_EXIT_CODE == 4
    assert plan.unmapped_paths == ["synthetic/unmapped_contract.py"]
    assert plan.selected_tests == []
