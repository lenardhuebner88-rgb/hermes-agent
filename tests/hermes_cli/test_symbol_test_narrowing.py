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


def test_all_32_objpatch_only_symbols_remain_tested(real_indexes) -> None:
    _, symbol_index = real_indexes
    symbols = {
        channel: set(symbol_index.by_channel[channel])
        for channel in REFERENCE_CHANNELS
    }
    objpatch_only = symbols["objpatch"] - (
        symbols["attr"] | symbols["strpatch"] | symbols["direct"]
    )

    assert len(objpatch_only) == 32
    assert "_launch_worker_process" in objpatch_only
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


def test_cli_passes_its_ref_to_symbol_narrowing(tmp_path: Path) -> None:
    _create_fanout_repo(tmp_path)
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
