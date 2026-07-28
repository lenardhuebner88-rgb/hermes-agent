"""Fail-broad symbol-level narrowing for broad affected-test import matches."""
from __future__ import annotations

import ast
import re
import warnings
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol


REFERENCE_CHANNELS = ("attr", "objpatch", "strpatch", "direct")
_HUNK_HEADER_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)


class GitRunner(Protocol):
    def __call__(self, repo_root: Path, *args: str) -> str: ...


@dataclass(frozen=True)
class SymbolDiffSpec:
    """The exact left/right refs used to discover the changed path set."""

    ref: str | None
    right: str | None = None
    default_base: str | None = None


@dataclass(frozen=True, order=True)
class SymbolReference:
    module: str
    symbol: str
    channel: str


@dataclass(frozen=True)
class SymbolRange:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class SymbolTestIndex:
    module_import: str
    by_symbol: Mapping[str, tuple[str, ...]]
    by_channel: Mapping[str, Mapping[str, tuple[str, ...]]]
    unreadable_tests: tuple[str, ...]


@dataclass(frozen=True)
class SymbolNarrowingResult:
    tests: tuple[str, ...]
    applied: bool
    reason: str
    changed_symbols: tuple[str, ...] = ()


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
    return None


def _import_context(
    tree: ast.AST,
) -> tuple[
    dict[str, set[str]],
    set[str],
    set[SymbolReference],
]:
    aliases: dict[str, set[str]] = defaultdict(set)
    qualified_modules: set[str] = set()
    references: set[SymbolReference] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                qualified_modules.add(alias.name)
                if alias.asname:
                    aliases[alias.asname].add(alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
        ):
            for alias in node.names:
                if alias.name == "*":
                    continue
                references.add(
                    SymbolReference(node.module, alias.name, "direct")
                )
                imported_name = f"{node.module}.{alias.name}"
                aliases[alias.asname or alias.name].add(imported_name)
                qualified_modules.add(imported_name)
    return aliases, qualified_modules, references


def _references_for_tree(tree: ast.AST) -> frozenset[SymbolReference]:
    aliases, qualified_modules, references = _import_context(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                for module in aliases.get(node.value.id, ()):
                    references.add(SymbolReference(module, node.attr, "attr"))

            dotted = _dotted_name(node)
            if dotted:
                for module in qualified_modules:
                    prefix = f"{module}."
                    if dotted.startswith(prefix) and "." not in dotted[len(prefix) :]:
                        references.add(
                            SymbolReference(module, dotted[len(prefix) :], "attr")
                        )

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            module, separator, symbol = node.value.rpartition(".")
            if separator and "." in module and symbol:
                references.add(SymbolReference(module, symbol, "strpatch"))

        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            function = node.func
            is_object_patch = (
                isinstance(function, ast.Name) and function.id in {"setattr", "delattr"}
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr in {"setattr", "delattr"}
            )
            target, name = node.args[:2]
            if (
                is_object_patch
                and isinstance(target, ast.Name)
                and isinstance(name, ast.Constant)
                and isinstance(name.value, str)
            ):
                for module in aliases.get(target.id, ()):
                    references.add(
                        SymbolReference(module, name.value, "objpatch")
                    )

    return frozenset(references)


@lru_cache(maxsize=8192)
def _cached_test_symbol_references(
    path_value: str,
    mtime_ns: int,
    size: int,
) -> frozenset[SymbolReference] | None:
    del mtime_ns, size
    path = Path(path_value)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    return _references_for_tree(tree)


def _test_symbol_references(path: Path) -> frozenset[SymbolReference] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _cached_test_symbol_references(
        str(path.resolve()),
        stat.st_mtime_ns,
        stat.st_size,
    )


def build_symbol_test_index(
    repo_root: Path,
    module_import: str,
    test_paths: Iterable[str],
) -> SymbolTestIndex:
    """Build a symbol-to-test index for one module over the repository test set."""

    by_channel_sets: dict[str, dict[str, set[str]]] = {
        channel: defaultdict(set) for channel in REFERENCE_CHANNELS
    }
    unreadable: set[str] = set()

    for relative in sorted(set(test_paths)):
        references = _test_symbol_references(repo_root / relative)
        if references is None:
            unreadable.add(relative)
            continue
        for reference in references:
            if (
                reference.module == module_import
                and reference.channel in by_channel_sets
            ):
                by_channel_sets[reference.channel][reference.symbol].add(relative)

    by_channel: dict[str, dict[str, tuple[str, ...]]] = {}
    by_symbol_sets: dict[str, set[str]] = defaultdict(set)
    for channel in REFERENCE_CHANNELS:
        channel_index = {
            symbol: tuple(sorted(paths))
            for symbol, paths in sorted(by_channel_sets[channel].items())
        }
        by_channel[channel] = channel_index
        for symbol, paths in channel_index.items():
            by_symbol_sets[symbol].update(paths)

    return SymbolTestIndex(
        module_import=module_import,
        by_symbol={
            symbol: tuple(sorted(paths))
            for symbol, paths in sorted(by_symbol_sets.items())
        },
        by_channel=by_channel,
        unreadable_tests=tuple(sorted(unreadable)),
    )


def _assignment_names(target: ast.AST) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(
            name
            for element in target.elts
            for name in _assignment_names(element)
        )
    return ()


def top_level_symbol_ranges(source: str) -> tuple[SymbolRange, ...] | None:
    """Return top-level symbol ranges, or ``None`` when the AST is invalid."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    ranges: list[SymbolRange] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min(
                (node.lineno, *(decorator.lineno for decorator in node.decorator_list))
            )
            ranges.append(
                SymbolRange(
                    name=node.name,
                    start=start,
                    end=node.end_lineno or node.lineno,
                )
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                ranges.extend(
                    SymbolRange(
                        name=name,
                        start=node.lineno,
                        end=node.end_lineno or node.lineno,
                    )
                    for name in _assignment_names(target)
                )
        elif isinstance(node, ast.AnnAssign):
            ranges.extend(
                SymbolRange(
                    name=name,
                    start=node.lineno,
                    end=node.end_lineno or node.lineno,
                )
                for name in _assignment_names(node.target)
            )
    return tuple(ranges)


def _changed_after_lines(diff: str) -> set[int]:
    lines: set[int] = set()
    for line in diff.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            continue
        start = int(match.group("start"))
        count_text = match.group("count")
        count = int(count_text) if count_text is not None else 1
        # A pure deletion has no surviving after-line. Use Git's after-side
        # anchor, matching the measurement oracle, to retain its containing symbol.
        lines.update(range(start, start + max(count, 1)))
    return lines


def _broad(
    imported: tuple[str, ...],
    reason: str,
    changed_symbols: Iterable[str] = (),
) -> SymbolNarrowingResult:
    return SymbolNarrowingResult(
        tests=imported,
        applied=False,
        reason=reason,
        changed_symbols=tuple(sorted(changed_symbols)),
    )


def narrow_imported_tests(
    *,
    repo_root: Path,
    source_path: str,
    module_import: str,
    imported_tests: Iterable[str],
    all_test_paths: Iterable[str],
    threshold: int,
    diff_spec: SymbolDiffSpec | None,
    run_git: GitRunner,
    git_error_type: type[Exception],
    git_timeout_error_type: type[Exception],
) -> SymbolNarrowingResult:
    """Narrow a broad import match, always falling back to the input on doubt."""

    imported = tuple(sorted(set(imported_tests)))
    if len(imported) <= threshold:
        return _broad(imported, "below_fanout_threshold")
    if diff_spec is None:
        return _broad(imported, "missing_diff_context")

    try:
        if diff_spec.right is not None:
            diff = run_git(
                repo_root,
                "diff",
                "--diff-filter=ACDMRT",
                "--unified=0",
                diff_spec.ref or "HEAD",
                diff_spec.right,
                "--",
                source_path,
            )
        elif diff_spec.ref:
            diff = run_git(
                repo_root,
                "diff",
                "--diff-filter=ACDMRT",
                "--unified=0",
                diff_spec.ref,
                "--",
                source_path,
            )
        else:
            if diff_spec.default_base is None:
                return _broad(imported, "missing_default_base")
            diff = run_git(
                repo_root,
                "diff",
                "--diff-filter=ACDMRT",
                "--unified=0",
                diff_spec.default_base,
                "--",
                source_path,
            )
    except git_timeout_error_type:
        raise
    except git_error_type:
        return _broad(imported, "diff_unavailable")

    changed_lines = _changed_after_lines(diff)
    if not changed_lines:
        return _broad(imported, "no_changed_lines")

    if diff_spec.right is not None:
        try:
            source = run_git(
                repo_root,
                "show",
                f"{diff_spec.right}:{source_path}",
            )
        except git_timeout_error_type:
            raise
        except git_error_type:
            return _broad(imported, "after_source_unavailable")
    else:
        try:
            source = (repo_root / source_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _broad(imported, "after_source_unavailable")

    ranges = top_level_symbol_ranges(source)
    if ranges is None:
        return _broad(imported, "unparseable_after_ast")
    changed_symbols = {
        symbol_range.name
        for symbol_range in ranges
        if any(
            symbol_range.start <= line <= symbol_range.end
            for line in changed_lines
        )
    }
    if not changed_symbols:
        return _broad(imported, "no_changed_symbols")

    symbol_index = build_symbol_test_index(
        repo_root,
        module_import,
        all_test_paths,
    )
    if symbol_index.unreadable_tests:
        return _broad(imported, "unreadable_test_ast", changed_symbols)

    matched = {
        test
        for symbol in changed_symbols
        for test in symbol_index.by_symbol.get(symbol, ())
    }
    narrowed = tuple(sorted(set(imported) & matched))
    if not narrowed:
        return _broad(imported, "no_symbol_test_matches", changed_symbols)

    return SymbolNarrowingResult(
        tests=narrowed,
        applied=True,
        reason="symbol_matches",
        changed_symbols=tuple(sorted(changed_symbols)),
    )
