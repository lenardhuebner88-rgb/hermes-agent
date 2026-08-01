from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli.affected_test_mapping import (
    GitTimeoutError,
    MappingError,
    SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD,
    _default_diff_base,
    _run_git,
    build_test_index,
    changed_paths_with_diff_spec,
)
from hermes_cli.symbol_test_narrowing import (
    REFERENCE_CHANNELS,
    SymbolDiffSpec,
    SymbolNarrowingResult,
    build_symbol_test_index,
    narrow_imported_tests,
    top_level_symbol_ranges,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
KANBAN_MODULE = "hermes_cli.kanban_db"
KANBAN_PATH = "hermes_cli/kanban_db.py"
WEB_MODULE = "hermes_cli.web_server"
WEB_PATH = "hermes_cli/web_server.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _create_fanout_repo(
    repo: Path,
    *,
    include_source: bool = True,
    test_count: int = SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1,
) -> str:
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    if include_source:
        (repo / "pkg" / "runtime.py").write_text(
            "def target():\n"
            "    return 1\n",
            encoding="utf-8",
        )
    tests = repo / "tests"
    tests.mkdir()
    for index in range(test_count):
        (tests / f"test_runtime_{index:03d}.py").write_text(
            "from pkg import runtime as kb\n\n"
            f"def test_runtime_{index:03d}():\n"
            "    assert kb.target() == 1\n",
            encoding="utf-8",
        )
    return _commit(repo, "baseline")


def _narrow(
    repo: Path,
    *,
    module_import: str = "pkg.runtime",
    source_path: str = "pkg/runtime.py",
    diff_spec: SymbolDiffSpec,
    imported_tests: tuple[str, ...] | None = None,
) -> SymbolNarrowingResult:
    test_index = build_test_index(repo)
    imported = (
        imported_tests
        if imported_tests is not None
        else test_index.imports[module_import]
    )
    return narrow_imported_tests(
        repo_root=repo,
        source_path=source_path,
        module_import=module_import,
        imported_tests=imported,
        all_test_paths=test_index.paths,
        threshold=SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD,
        diff_spec=diff_spec,
        run_git=_run_git,
        git_error_type=MappingError,
        git_timeout_error_type=GitTimeoutError,
    )


def _narrow_real_web_source_diff(
    test_index,
    *,
    source: str,
    diff: str,
) -> SymbolNarrowingResult:
    def run_git(_repo_root: Path, *args: str) -> str:
        if args[0] == "diff":
            return diff
        if args[0] == "show":
            return source
        raise AssertionError(f"unexpected git call: {args}")

    return narrow_imported_tests(
        repo_root=REPO_ROOT,
        source_path=WEB_PATH,
        module_import=WEB_MODULE,
        imported_tests=test_index.imports[WEB_MODULE],
        all_test_paths=test_index.paths,
        threshold=SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD,
        diff_spec=SymbolDiffSpec(ref="before", right="after"),
        run_git=run_git,
        git_error_type=MappingError,
        git_timeout_error_type=GitTimeoutError,
    )


@pytest.fixture(scope="module")
def real_indexes():
    test_index = build_test_index(REPO_ROOT)
    symbol_index = build_symbol_test_index(
        REPO_ROOT,
        KANBAN_MODULE,
        test_index.paths,
    )
    return test_index, symbol_index


@pytest.mark.parametrize(
    ("channel", "symbol", "test_path"),
    [
        ("attr", "connect", "tests/gateway/test_kanban_alerts.py"),
        (
            "objpatch",
            "ack_notify_delivery_claim",
            "tests/gateway/test_kanban_notifier_lease_loss_isolation.py",
        ),
        (
            "strpatch",
            "_record_task_failure",
            "tests/agent/test_turn_finalizer_iteration_limit_exit.py",
        ),
        (
            "direct",
            "_check_file_length_invariant",
            "tests/hermes_cli/test_kanban_db_runtime.py",
        ),
    ],
)
def test_real_reference_channels_are_indexed(
    real_indexes,
    channel: str,
    symbol: str,
    test_path: str,
) -> None:
    _, symbol_index = real_indexes

    assert test_path in symbol_index.by_channel[channel][symbol]


def test_exact_objpatch_only_symbol_set_remains_tested_after_channel_growth(
    real_indexes,
) -> None:
    _, symbol_index = real_indexes
    symbols = {
        channel: set(symbol_index.by_channel[channel])
        for channel in REFERENCE_CHANNELS
    }
    objpatch_only = symbols["objpatch"] - (
        symbols["attr"] | symbols["strpatch"] | symbols["direct"]
    )

    assert objpatch_only == {
        "KANBAN_ARTIFACT_TREE_MAX_ENTRIES",
        "KANBAN_ATTACHMENT_MAX_BYTES",
        "_CORRUPT_BACKUP_RETENTION",
        "_INIT_LOCK_TIMEOUT_SECONDS",
        "_IS_WINDOWS",
        "_active_lane_entry_for_profile_from_conn",
        "_attach_or_reap_spawned_worker",
        "_attempt_index_reindex_repair",
        "_budget_extension_config",
        "_evidence_freshness_preflight_enabled",
        "_git_commit_distance",
        "_git_head_sha_for_workspace",
        "_is_claude_cli_runtime",
        "_lane_provider_model_for_profile",
        "_persisted_spawn_identity",
        "_profile_model_provider_for_spawn",
        "_read_profile_provider",
        "_release_freigabe_hold_root_in_txn",
        "_render_knowledge_pointers",
        "_resolve_dispatch_workspace",
        "_run_is_claude_cli",
        "_skill_available_for_home",
        "_systemd_scope_usable",
        "_worker_brief_input",
        "_worker_process_group_alive",
        "_worker_project_context_cwd",
        "_workspace_release_state",
        "estimate_equivalent_cost_amount",
        "estimate_equivalent_cost_amounts",
        "validate_spawnable_assignee",
        "vault_memory_links_for_task",
    }
    # 860dbfe178 added real ``kb._launch_worker_process(...)`` calls.  The
    # launcher is still covered through objpatch references, but is no longer
    # objpatch-only because the new runtime-facts regression tests exercise the
    # production attribute directly.  Pin both channels so a future count
    # change cannot be "fixed" by silently deleting either form of evidence.
    assert "_launch_worker_process" not in objpatch_only
    assert symbol_index.by_channel["attr"]["_launch_worker_process"] == (
        "tests/hermes_cli/test_kanban_runtime_facts.py",
    )
    assert set(symbol_index.by_channel["objpatch"]["_launch_worker_process"]) == {
        "tests/hermes_cli/test_kanban_db_tokens_workspace.py",
        "tests/hermes_cli/test_kanban_provider_override_dispatch_fork.py",
    }
    for symbol in objpatch_only:
        assert symbol_index.by_channel["objpatch"][symbol]
        assert symbol_index.by_symbol[symbol]


def test_fixture_parameter_channel_is_deliberately_not_built() -> None:
    assert REFERENCE_CHANNELS == ("attr", "objpatch", "strpatch", "direct")


def test_every_real_module_at_or_below_threshold_is_unchanged(
    real_indexes,
) -> None:
    test_index, _ = real_indexes
    checked = 0

    for module_import, imported in test_index.imports.items():
        if len(imported) > SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD:
            continue
        result = narrow_imported_tests(
            repo_root=REPO_ROOT,
            source_path="unused.py",
            module_import=module_import,
            imported_tests=imported,
            all_test_paths=test_index.paths,
            threshold=SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD,
            diff_spec=SymbolDiffSpec(ref="unused"),
            run_git=_run_git,
            git_error_type=MappingError,
            git_timeout_error_type=GitTimeoutError,
        )
        assert result.tests == imported
        assert result.applied is False
        assert result.reason == "below_fanout_threshold"
        checked += 1

    assert checked > 0


def test_ref_right_ref_only_and_default_diff_use_after_version(
    tmp_path: Path,
) -> None:
    base = _create_fanout_repo(tmp_path)
    source = tmp_path / "pkg" / "runtime.py"
    source.write_text("def target():\n    return 2\n", encoding="utf-8")
    right = _commit(tmp_path, "right")

    between_refs = _narrow(
        tmp_path,
        diff_spec=SymbolDiffSpec(ref=base, right=right),
    )

    source.write_text("def target():\n    return 3\n", encoding="utf-8")
    from_ref = _narrow(
        tmp_path,
        diff_spec=SymbolDiffSpec(ref=right),
    )
    _, default_diff_spec = changed_paths_with_diff_spec(tmp_path, None)
    from_default_base = _narrow(
        tmp_path,
        diff_spec=default_diff_spec,
    )

    for result in (between_refs, from_ref, from_default_base):
        assert result.applied is True
        assert result.reason == "symbol_matches"
        assert result.changed_symbols == ("target",)
        assert len(result.tests) == SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1


def test_cli_passes_its_ref_to_symbol_narrowing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_fanout_repo(tmp_path)
    monkeypatch.setenv("HERMES_AFFECTED_BUDGET_OK", "1")
    (tmp_path / "pkg" / "runtime.py").write_text(
        "def target():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "affected_tests.py"),
            "--format",
            "json",
            "HEAD",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    record = json.loads(completed.stdout)["records"][0]

    assert record["path"] == "pkg/runtime.py"
    assert record["strategies"] == ["import→symbol"]


def test_untracked_new_production_file_does_not_narrow(tmp_path: Path) -> None:
    _create_fanout_repo(tmp_path, include_source=False)
    (tmp_path / "pkg" / "runtime.py").write_text(
        "def target():\n"
        "    return 1\n",
        encoding="utf-8",
    )

    result = _narrow(
        tmp_path,
        diff_spec=SymbolDiffSpec(
            ref=None,
            default_base=_default_diff_base(tmp_path),
        ),
    )

    assert result.applied is False
    assert result.reason == "no_changed_lines"
    assert len(result.tests) == SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1


def test_changed_lines_outside_symbols_do_not_narrow_real_commit(
    real_indexes,
) -> None:
    test_index, _ = real_indexes
    imported = test_index.imports[KANBAN_MODULE]

    result = _narrow(
        REPO_ROOT,
        module_import=KANBAN_MODULE,
        source_path=KANBAN_PATH,
        diff_spec=SymbolDiffSpec(ref="c1f623fcc^", right="c1f623fcc"),
        imported_tests=imported,
    )

    assert result.applied is False
    assert result.reason == "no_changed_symbols"
    assert result.changed_symbols == ()
    assert result.tests == imported


def test_real_additive_web_route_registration_selects_registered_module_test(
    real_indexes,
) -> None:
    test_index, _ = real_indexes

    result = _narrow(
        REPO_ROOT,
        module_import=WEB_MODULE,
        source_path=WEB_PATH,
        diff_spec=SymbolDiffSpec(ref="aae51569e8^", right="aae51569e8"),
        imported_tests=test_index.imports[WEB_MODULE],
    )

    assert len(test_index.imports[WEB_MODULE]) == 66
    assert result == SymbolNarrowingResult(
        tests=("tests/hermes_cli/test_buzz_agent_cleanup.py",),
        applied=True,
        reason="module_registration_matches",
    )


@pytest.mark.parametrize("change", ["delete", "modify"])
def test_real_module_level_route_removal_or_change_stays_broad(
    real_indexes,
    change: str,
) -> None:
    test_index, _ = real_indexes
    lines = (REPO_ROOT / WEB_PATH).read_text(encoding="utf-8").splitlines()
    line_number = lines.index("register_buzz_agent_cleanup_routes(app)") + 1
    old_line = lines[line_number - 1]
    if change == "delete":
        del lines[line_number - 1]
        hunk = f"@@ -{line_number},1 +{line_number - 1},0 @@\n-{old_line}\n"
    else:
        new_line = "register_buzz_agent_cleanup_routes(app, enabled=True)"
        lines[line_number - 1] = new_line
        hunk = (
            f"@@ -{line_number},1 +{line_number},1 @@\n"
            f"-{old_line}\n+{new_line}\n"
        )

    result = _narrow_real_web_source_diff(
        test_index,
        source="\n".join(lines) + "\n",
        diff=hunk,
    )

    assert result.applied is False
    assert result.reason == "no_changed_symbols"
    assert len(result.tests) == 66


def test_additive_registration_without_module_test_stays_broad(
    real_indexes,
) -> None:
    test_index, _ = real_indexes
    lines = (REPO_ROOT / WEB_PATH).read_text(encoding="utf-8").splitlines()
    line_number = lines.index("mount_spa(app)") + 1
    additions = (
        "from hermes_cli.uncovered_routes import register_uncovered_routes",
        "register_uncovered_routes(app)",
    )
    lines[line_number - 1 : line_number - 1] = additions
    hunk = (
        f"@@ -{line_number - 1},0 +{line_number},{len(additions)} @@\n"
        + "".join(f"+{line}\n" for line in additions)
    )

    result = _narrow_real_web_source_diff(
        test_index,
        source="\n".join(lines) + "\n",
        diff=hunk,
    )

    assert result.applied is False
    assert result.reason == "no_changed_symbols"
    assert len(result.tests) == 66


def test_unparseable_after_ast_does_not_narrow(tmp_path: Path) -> None:
    _create_fanout_repo(tmp_path)
    (tmp_path / "pkg" / "runtime.py").write_text(
        "def target(:\n",
        encoding="utf-8",
    )

    result = _narrow(
        tmp_path,
        diff_spec=SymbolDiffSpec(
            ref=None,
            default_base=_default_diff_base(tmp_path),
        ),
    )

    assert result.applied is False
    assert result.reason == "unparseable_after_ast"
    assert len(result.tests) == SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD + 1


def test_resolved_symbol_without_test_match_returns_empty_selection(
    tmp_path: Path,
) -> None:
    _create_fanout_repo(tmp_path)
    (tmp_path / "pkg" / "runtime.py").write_text(
        "def target():\n"
        "    return 1\n\n"
        "def unreferenced():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    result = _narrow(
        tmp_path,
        diff_spec=SymbolDiffSpec(
            ref=None,
            default_base=_default_diff_base(tmp_path),
        ),
    )

    assert result.applied is True
    assert result.reason == "no_symbol_test_matches"
    assert result.changed_symbols == ("unreferenced",)
    assert result.tests == ()


def test_unreadable_test_ast_keeps_the_full_import_set(tmp_path: Path) -> None:
    _create_fanout_repo(tmp_path)
    broken_test = tmp_path / "tests" / "test_runtime_broken.py"
    broken_test.write_text(
        "from pkg import runtime as kb\n"
        "def broken(:\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "runtime.py").write_text(
        "def target():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    imported = tuple(
        sorted(
            (
                *build_test_index(tmp_path).imports["pkg.runtime"],
                "tests/test_runtime_broken.py",
            )
        )
    )

    result = _narrow(
        tmp_path,
        diff_spec=SymbolDiffSpec(
            ref=None,
            default_base=_default_diff_base(tmp_path),
        ),
        imported_tests=imported,
    )

    assert result.applied is False
    assert result.reason == "unreadable_test_ast"
    assert result.tests == imported


def test_real_historical_commit_selects_expected_symbol_scale(
    real_indexes,
) -> None:
    test_index, _ = real_indexes

    result = _narrow(
        REPO_ROOT,
        module_import=KANBAN_MODULE,
        source_path=KANBAN_PATH,
        diff_spec=SymbolDiffSpec(ref="15ac3b65d^", right="15ac3b65d"),
        imported_tests=test_index.imports[KANBAN_MODULE],
    )

    assert result.applied is True
    assert result.changed_symbols
    assert len(result.tests) == 1


def test_decorators_and_top_level_assignments_extend_symbol_ranges() -> None:
    ranges = top_level_symbol_ranges(
        "@decorate(\n"
        "    option=True,\n"
        ")\n"
        "def decorated():\n"
        "    return 1\n\n"
        "FIRST = SECOND = (\n"
        "    1\n"
        ")\n"
        "LEFT, RIGHT = (2, 3)\n"
    )

    assert ranges is not None
    by_name = {symbol_range.name: symbol_range for symbol_range in ranges}
    assert by_name["decorated"].start == 1
    assert by_name["decorated"].end == 5
    assert {name for name in by_name if name != "decorated"} == {
        "FIRST",
        "SECOND",
        "LEFT",
        "RIGHT",
    }


def test_symbol_reference_cache_invalidates_on_file_change(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "tests" / "test_runtime.py"
    test_path.parent.mkdir()
    test_path.write_text(
        "from pkg import runtime as kb\nVALUE = kb.FIRST\n",
        encoding="utf-8",
    )
    first = build_symbol_test_index(
        tmp_path,
        "pkg.runtime",
        ("tests/test_runtime.py",),
    )
    previous = test_path.stat()
    test_path.write_text(
        "from pkg import runtime as kb\nVALUE = kb.OTHER\n",
        encoding="utf-8",
    )
    os.utime(
        test_path,
        ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000),
    )

    second = build_symbol_test_index(
        tmp_path,
        "pkg.runtime",
        ("tests/test_runtime.py",),
    )

    assert "FIRST" in first.by_symbol
    assert "FIRST" not in second.by_symbol
    assert "OTHER" in second.by_symbol
