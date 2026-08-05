from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

import hermes_cli.affected_test_mapping as affected_test_mapping
from hermes_cli.affected_test_budget import (
    AFFECTED_BUDGET_OVERRIDE_ENV,
    AffectedTestBudgetCheck,
    check_affected_test_budget,
)
from hermes_cli.affected_test_mapping import (
    AFFECTED_TIME_BUDGET_EXIT_CODE,
    AffectedTestBudgetExceeded,
    EXPLICIT_TEST_PATTERNS,
    INTEGRATION_FALLBACK_MAX_TEST_FILES,
    MappingError,
    SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD,
    UNMAPPED_EXIT_CODE,
    WORKER_FALLBACK_MAX_TEST_FILES,
    affected_pytest_modules,
    build_test_index,
    changed_paths,
    census_repository,
    classify_changed_paths,
)
from hermes_cli.symbol_test_narrowing import SymbolDiffSpec


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_mapping_contracts_from_operational_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_AFFECTED_TIME_BUDGET", "1000000")
    monkeypatch.setenv(AFFECTED_BUDGET_OVERRIDE_ENV, "1")


@pytest.fixture(scope="module")
def real_test_index():
    return build_test_index(REPO_ROOT)


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


def test_affected_pytest_modules_forwards_explicit_budget_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "feature.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_feature.py").write_text("")
    _init_repo(tmp_path)
    requested_env = {"HERMES_AFFECTED_TIME_BUDGET": "3600"}
    captured: dict[str, object] = {}

    def fake_budget(repo_root, selected_targets, *, durations_path=None, env=None):
        captured["repo_root"] = repo_root
        captured["selected_targets"] = list(selected_targets)
        captured["env"] = env
        return AffectedTestBudgetCheck(estimate=None)

    monkeypatch.setattr(
        affected_test_mapping,
        "check_affected_test_budget",
        fake_budget,
    )

    modules = affected_pytest_modules(
        tmp_path,
        ["pkg/feature.py"],
        budget_env=requested_env,
    )

    assert modules == ["tests/pkg/test_feature.py"]
    assert captured == {
        "repo_root": tmp_path,
        "selected_targets": ["tests/pkg/test_feature.py"],
        "env": requested_env,
    }


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


def test_real_historical_diff_exposes_symbol_narrowing_strategy() -> None:
    index = build_test_index(REPO_ROOT)

    record = classify_changed_paths(
        REPO_ROOT,
        ["hermes_cli/kanban_db.py"],
        mode="integration",
        index=index,
        diff_spec=SymbolDiffSpec(ref="15ac3b65d^", right="15ac3b65d"),
    ).records[0]

    assert SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD == 60
    assert "import→symbol" in record.strategies
    assert "import" not in record.strategies
    assert record.state == "selected"


def test_real_uncovered_symbol_selects_curated_tests_and_warns(
    real_test_index,
) -> None:
    plan = classify_changed_paths(
        REPO_ROOT,
        ["hermes_cli/kanban_db.py"],
        mode="integration",
        index=real_test_index,
        diff_spec=SymbolDiffSpec(ref="7f5e4f848^", right="7f5e4f848"),
    )
    record = plan.records[0]
    expected_tests = {
        str(path.relative_to(REPO_ROOT))
        for pattern in EXPLICIT_TEST_PATTERNS["hermes_cli/kanban_db.py"]
        for path in REPO_ROOT.glob(pattern)
        if path.is_file()
    }

    # 30 seit dem Upstream-Vollmerge 2026-08-04: die 8 fork-eigenen Run-Metric-
    # Tests zogen aus der upstream-eigenen tests/hermes_cli/test_kanban_db.py in
    # tests/hermes_cli/test_kanban_db_run_metrics_fork.py, damit Upstreams 230
    # Tests dort wieder vollstaendig stehen koennen.
    assert len(expected_tests) == 30
    assert record.state == "selected"
    assert set(record.tests) == expected_tests
    assert record.warnings == (
        "symbol coverage gap for hermes_cli/kanban_db.py: changed symbol without "
        "test references: _run_evidence_freshness_preflight; the 30 curated/direct "
        "test files for this path ran instead of the module-level import set and "
        "the affected-test gate intentionally remains non-red",
    )
    assert plan.unmapped_paths == []
    assert UNMAPPED_EXIT_CODE == 4


def test_real_diff_outside_symbols_keeps_module_imports_without_a4_warning(
    real_test_index,
) -> None:
    record = classify_changed_paths(
        REPO_ROOT,
        ["hermes_cli/kanban_db.py"],
        mode="integration",
        index=real_test_index,
        diff_spec=SymbolDiffSpec(ref="c1f623fcc^", right="c1f623fcc"),
    ).records[0]

    assert "import" in record.strategies
    assert "import→symbol" not in record.strategies
    assert set(real_test_index.imports["hermes_cli.kanban_db"]).issubset(record.tests)
    assert record.warnings == ()


def test_mixed_referenced_and_unreferenced_symbols_do_not_warn(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pkg" / "runtime.py"
    source.parent.mkdir()
    source.write_text(
        "def covered():\n"
        "    return 1\n\n"
        "def uncovered():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    for index in range(SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1):
        (tests / f"test_runtime_{index:03d}.py").write_text(
            "from pkg import runtime\n\n"
            f"def test_runtime_{index:03d}():\n"
            "    assert runtime.covered() == 1\n",
            encoding="utf-8",
        )
    _init_repo(tmp_path)
    source.write_text(
        "def covered():\n"
        "    return 2\n\n"
        "def uncovered():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    record = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="integration",
        diff_spec=SymbolDiffSpec(ref="HEAD"),
    ).records[0]

    assert record.state == "selected"
    assert record.strategies == ("import→symbol",)
    assert len(record.tests) == SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1
    assert record.warnings == ()


def test_uncovered_symbol_without_curated_or_direct_net_keeps_module_imports(
    tmp_path: Path,
) -> None:
    """A named gap must never turn into a green run over zero test files.

    Freigabe 3 ("run the curated EXPLICIT_TEST_PATTERNS plus a loud warning")
    presumes a curated net exists. Measured 2026-07-28: 6 of the 14 modules above
    the fan-out threshold have neither a curated pattern nor a direct test
    (gateway/run.py, gateway/platforms/base.py, run_agent.py, cli.py,
    hermes_cli/main.py, hermes_cli/auth.py). Without the fallback below, a change
    to an untested symbol in those selects nothing at all and still passes.
    """
    source = tmp_path / "pkg" / "runtime.py"
    source.parent.mkdir()
    source.write_text(
        "def covered():\n"
        "    return 1\n\n"
        "def uncovered():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    # No tests/pkg/test_runtime.py => no `direct` hit; pkg/runtime.py has no
    # EXPLICIT_TEST_PATTERNS entry => no curated hit. Only the import fan-out.
    for index in range(SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1):
        (tests / f"test_runtime_{index:03d}.py").write_text(
            "from pkg import runtime\n\n"
            f"def test_runtime_{index:03d}():\n"
            "    assert runtime.covered() == 1\n",
            encoding="utf-8",
        )
    _init_repo(tmp_path)
    # Change ONLY the symbol no test references.
    source.write_text(
        "def covered():\n"
        "    return 1\n\n"
        "def uncovered():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    record = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="integration",
        diff_spec=SymbolDiffSpec(ref="HEAD"),
    ).records[0]

    assert record.state == "selected"
    # The module-level set survives instead of collapsing to zero files.
    assert len(record.tests) == SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1
    assert record.strategies == ("import",)
    assert len(record.warnings) == 1
    warning = record.warnings[0]
    assert "symbol coverage gap for pkg/runtime.py" in warning
    assert "uncovered" in warning
    # The message must not claim a curated net that does not exist.
    assert "no curated or direct test covers this path" in warning
    assert "EXPLICIT_TEST_PATTERNS ran" not in warning


@pytest.mark.parametrize(
    "changed",
    [
        ".claude/skills/hermes-loops/SKILL.md",
        "loops/packs/dashboard-polish/BUILDER-PROMPT.md",
    ],
)
def test_skill_and_prompt_changes_select_the_hygiene_gate(changed: str) -> None:
    """The hygiene checker must run on the very files it polices.

    Measured 2026-07-28 against the real tree: before the two
    FEATURE_PREFIX_TEST_PATTERNS entries, a SKILL.md or *-PROMPT.md change was
    classified not_applicable/non_python and selected zero test files. The
    checker therefore only ever ran when someone edited the checker itself, so a
    skill pointing at a dead path stayed green — exactly the silent failure the
    gate exists to prevent.
    """
    assert (REPO_ROOT / changed).is_file(), f"fixture path vanished: {changed}"

    record = classify_changed_paths(REPO_ROOT, [changed], mode="worker").records[0]

    assert record.state == "selected"
    assert record.scope == "gate_control"
    assert "tests/scripts/test_check_skill_hygiene.py" in record.tests


def test_lifecycle_document_change_selects_the_anchor_gate() -> None:
    changed = "docs/kanban/LIFECYCLE.md"
    assert (REPO_ROOT / changed).is_file(), f"fixture path vanished: {changed}"

    record = classify_changed_paths(REPO_ROOT, [changed], mode="worker").records[0]

    assert record.state == "selected"
    assert record.scope == "gate_control"
    assert record.tests == (
        "tests/scripts/test_check_kanban_lifecycle_anchors.py",
    )


@pytest.mark.parametrize(
    "changed",
    [
        "plugins/kanban/dashboard/planspec_flow_routes.py",
        "plugins/kanban/dashboard/control_routes.py",
    ],
)
def test_dashboard_route_module_change_selects_the_route_suite(changed: str) -> None:
    """The dashboard route tests must run on route-module changes.

    Twenty-two test files load plugins/kanban/dashboard/plugin_api.py via
    importlib.util.spec_from_file_location (twenty-one listed explicitly,
    one via the mirrored tests/plugins/kanban/dashboard/ glob), so the
    import index never sees the edge; plugin_api.py itself is covered by
    function-level static imports, but the route modules it includes via
    load_api_extension are not. Measured 2026-08-04 against the real tree:
    before the FEATURE_PREFIX_TEST_PATTERNS entry, a route-module change
    selected only the mirrored tests/plugins/kanban/dashboard/ directory,
    so the real route suite (tests/plugins/test_kanban_dashboard_plugin.py,
    /planspecs coverage) never ran on route changes. The four
    multi-line-join loaders below were caught by a second survey pass:
    they build the plugin path with one path component per source line,
    which a line-based grep misses. test_kanban_usage_facts_route.py
    imports plugin_api statically but asserts route-contract ownership of
    those dynamically loaded route modules, an edge the index equally
    cannot see.
    """
    assert (REPO_ROOT / changed).is_file(), f"fixture path vanished: {changed}"

    record = classify_changed_paths(REPO_ROOT, [changed], mode="worker").records[0]

    assert record.state == "selected"
    assert "tests/plugins/test_kanban_dashboard_plugin.py" in record.tests
    multi_line_join_loaders = {
        "tests/plugins/test_kanban_chain_summary_contract.py",
        "tests/plugins/test_kanban_chain_summary_query_budget.py",
        "tests/plugins/test_kanban_dispatch_cap_passthrough.py",
        "tests/plugins/test_kanban_task_detail_chain_context.py",
    }
    assert multi_line_join_loaders <= set(record.tests)
    assert "tests/plugins/test_kanban_usage_facts_route.py" in record.tests


def test_real_gateway_config_commit_does_not_warn(
    real_test_index,
) -> None:
    record = classify_changed_paths(
        REPO_ROOT,
        ["gateway/config.py"],
        mode="integration",
        index=real_test_index,
        diff_spec=SymbolDiffSpec(ref="9cd729684^", right="9cd729684"),
    ).records[0]

    assert record.state == "selected"
    assert record.tests
    assert "import→symbol" in record.strategies
    assert record.warnings == ()


def test_worker_union_budget_failure_is_deterministic_and_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration = classify_changed_paths(
        REPO_ROOT,
        ["hermes_cli/__init__.py"],
        mode="integration",
    ).records[0]
    cache = tmp_path / "durations.json"
    cache.write_text(
        json.dumps({path: 10.0 for path in integration.tests}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AFFECTED_TIME_BUDGET", "1")
    monkeypatch.setattr(
        affected_test_mapping,
        "check_affected_test_budget",
        lambda repo_root, targets, **_kwargs: check_affected_test_budget(
            repo_root,
            targets,
            durations_path=cache,
        ),
    )

    with pytest.raises(AffectedTestBudgetExceeded) as first:
        classify_changed_paths(
            REPO_ROOT,
            ["hermes_cli/__init__.py"],
            mode="worker",
        )
    with pytest.raises(AffectedTestBudgetExceeded) as repeated:
        classify_changed_paths(
            REPO_ROOT,
            ["hermes_cli/__init__.py"],
            mode="worker",
        )

    assert str(first.value) == str(repeated.value)
    assert first.value.estimate.file_count == len(integration.tests)
    assert first.value.estimate.file_count > 217
    assert "predicted loaded wall time" in str(first.value)
    assert "budget 1.0s" in str(first.value)
    assert "files without duration forecast" in str(first.value)
    assert "top-5 estimated files" in str(first.value)
    assert "discarded" not in str(first.value)


def test_worker_union_budget_failure_keeps_direct_explicit_import_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "pkg" / "runtime.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    tests = tmp_path / "tests"
    (tests / "pkg").mkdir(parents=True)
    (tests / "pkg" / "test_runtime.py").write_text(
        "def test_direct():\n    assert True\n"
    )
    (tests / "test_explicit.py").write_text(
        "def test_explicit():\n    assert True\n"
    )
    (tests / "test_importer.py").write_text(
        "from pkg import runtime\n\n"
        "def test_import():\n    assert runtime.VALUE\n"
    )
    _init_repo(tmp_path)
    monkeypatch.setitem(
        EXPLICIT_TEST_PATTERNS,
        "pkg/runtime.py",
        ("tests/test_explicit.py",),
    )
    (tmp_path / "test_durations.json").write_text(
        json.dumps(
            {
                "tests/pkg/test_runtime.py": 100.0,
                "tests/test_explicit.py": 100.0,
                "tests/test_importer.py": 100.0,
            }
        ),
        encoding="utf-8",
    )
    integration = classify_changed_paths(
        tmp_path,
        ["pkg/runtime.py"],
        mode="integration",
    ).records[0]

    monkeypatch.setenv("HERMES_AFFECTED_TIME_BUDGET", "1")
    with pytest.raises(AffectedTestBudgetExceeded) as caught:
        classify_changed_paths(
            tmp_path,
            ["pkg/runtime.py"],
            mode="worker",
        )

    assert set(integration.tests) == {
        "tests/pkg/test_runtime.py",
        "tests/test_explicit.py",
        "tests/test_importer.py",
    }
    assert caught.value.estimate.file_count == 3
    assert "3 selected test files" in str(caught.value)
    assert "0 files without duration forecast" in str(caught.value)
    assert "top-3 estimated files" in str(caught.value)
    assert "discarded" not in str(caught.value)


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
    assert AFFECTED_TIME_BUDGET_EXIT_CODE == 5
    assert plan.unmapped_paths == ["synthetic/unmapped_contract.py"]
    assert plan.selected_tests == []
