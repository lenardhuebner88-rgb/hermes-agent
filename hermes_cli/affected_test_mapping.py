"""Shared, fail-closed affected-test classification.

This module is deliberately pure stdlib.  It is imported both by the
standalone ``scripts/affected_tests.py`` entrypoint in bare worktrees and by
the post-merge integrator.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import warnings
from dataclasses import asdict, dataclass, replace
from datetime import date
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

from hermes_cli.affected_test_budget import (
    AffectedTestBudgetConfigError,
    AffectedTestTimeEstimate,
    check_affected_test_budget,
)
from hermes_cli.symbol_test_narrowing import (
    SymbolDiffSpec,
    narrow_imported_tests,
)

WORKER_FALLBACK_MAX_TEST_FILES = 200
INTEGRATION_FALLBACK_MAX_TEST_FILES = 800
# Import fan-out is the measured noise axis; modules at 60 files or below stay exact.
SYMBOL_NARROWING_IMPORT_FANOUT_THRESHOLD = 60
UNMAPPED_EXIT_CODE = 4
AFFECTED_TIME_BUDGET_EXIT_CODE = 5
EXCEPTIONS_PATH = Path("config/affected-test-exceptions.json")

_EXCEPTION_DISPOSITIONS = frozenset(
    {
        "covered_by_other_gate",
        "generated_entrypoint",
        "manual_only",
        "non_runtime_template",
    }
)

# These trees ship procedural/catalog payloads or documentation tooling, but
# they are not imported into the Hermes core Python runtime.  A file with a
# real discovered test is still selected before this rule is applied.
_NOT_APPLICABLE_PREFIXES = (
    "docs/",
    "optional-skills/",
    "skills/",
    "website/",
)

# Exact, maintained mappings for feature-split sources and the gate's own
# control files.  Every pattern is contract-tested to match at least one file
# in the canonical repository.
EXPLICIT_TEST_PATTERNS: dict[str, tuple[str, ...]] = {
    "agent/__init__.py": ("tests/agent/test_jiter_preload.py",),
    "agent/credential_persistence.py": ("tests/hermes_cli/test_auth_commands.py",),
    "agent/secret_sources/__init__.py": (
        "tests/test_*secrets.py",
        "tests/test_command_secret_source.py",
        "tests/test_env_loader_*secret*.py",
        "tests/secret_sources/test_*.py",
    ),
    "agent/secret_sources/_cache.py": (
        "tests/test_env_loader_*secret*.py",
        "tests/secret_sources/test_*.py",
    ),
    "agent/stream_diag.py": ("tests/run_agent/test_stream_drop_logging.py",),
    "config/affected-test-exceptions.json": (
        "tests/hermes_cli/test_affected_test_mapping.py",
    ),
    "docs/kanban/LIFECYCLE.md": (
        "tests/scripts/test_check_kanban_lifecycle_anchors.py",
    ),
    "gateway/__init__.py": (
        "tests/gateway/test_config.py",
        "tests/gateway/test_delivery.py",
        "tests/gateway/test_session.py",
    ),
    "gateway/authz_mixin.py": (
        "tests/gateway/test_config_driven_access_policy.py",
        "tests/gateway/test_multiplex_profile_authz.py",
        "tests/gateway/test_pairing_allowlist_bypass.py",
        "tests/gateway/test_relay_upstream_authz.py",
    ),
    "hermes_cli/__init__.py": ("tests/hermes_cli/test_ensure_utf8_locale.py",),
    "hermes_cli/affected_test_mapping.py": (
        "tests/hermes_cli/test_affected_test_mapping.py",
        "tests/scripts/test_affected_mapper_equivalence.py",
        "tests/scripts/test_affected_tests.py",
        "tests/scripts/test_run_affected*.py",
    ),
    "hermes_cli/kanban_breaker_message.py": (
        "tests/hermes_cli/test_kanban_breaker_gave_up_error.py",
    ),
    "hermes_cli/cli_billing_mixin.py": (
        "tests/hermes_cli/test_billing_cli.py",
        "tests/hermes_cli/test_subscription_cli.py",
    ),
    "hermes_cli/gateway_enroll.py": (
        "tests/hermes_cli/test_gateway.py",
        "tests/hermes_cli/test_subcommands_profile_gateway.py",
    ),
    "hermes_cli/strategist.py": ("tests/hermes_cli/test_strategist*.py",),
    "hermes_cli/kanban_db.py": (
        "tests/hermes_cli/test_kanban_db*.py",
        "tests/hermes_cli/test_kanban_lanes.py",
        "tests/hermes_cli/test_kanban_block_kind*.py",
        "tests/hermes_cli/test_kanban_provider_override*.py",
        "tests/plugins/test_kanban_model_override*.py",
        "tests/scripts/test_check_kanban_lifecycle_anchors.py",
    ),
    "hermes_cli/kanban_worktrees.py": (
        "tests/hermes_cli/test_kanban_worktrees*.py",
        "tests/hermes_cli/test_visual_gate.py",
        "tests/scripts/test_check_kanban_lifecycle_anchors.py",
    ),
    "hermes_cli/kanban_writer_leases.py": (
        "tests/hermes_cli/test_kanban_db_dispatcher.py",
    ),
    "hermes_cli/migrate.py": ("tests/hermes_cli/test_migrate_xai.py",),
    "hermes_cli/pairing.py": (
        "tests/gateway/test_pairing.py",
        "tests/gateway/test_pairing_allowlist_bypass.py",
    ),
    "hermes_cli/portal_cli.py": (
        "tests/hermes_cli/test_billing_cli.py",
        "tests/hermes_cli/test_status.py",
    ),
    "hermes_cli/promptforge_catalog.py": ("tests/test_promptforge.py",),
    "hermes_cli/route_identity.py": (
        "tests/run_agent/test_credential_rotation_route_settings.py",
        "tests/run_agent/test_switch_model_context.py",
    ),
    "hermes_cli/web_git.py": ("tests/hermes_cli/test_web_server_git.py",),
    "hermes_cli/windows_ssh_runtime.py": (
        "tests/hermes_cli/test_ssh_session_token_parser.py",
    ),
    "scripts/affected_tests.py": (
        "tests/hermes_cli/test_affected_test_mapping.py",
        "tests/scripts/test_affected_mapper_equivalence.py",
        "tests/scripts/test_affected_tests.py",
        "tests/scripts/test_run_affected*.py",
    ),
    "scripts/affected-tests.sh": (
        "tests/hermes_cli/test_run_affected_mapping.py",
        "tests/scripts/test_affected_mapper_equivalence.py",
        "tests/scripts/test_run_affected*.py",
    ),
    "scripts/check-branch-age.sh": (
        "tests/scripts/test_run_affected_exit_codes.py",
        "tests/test_check_branch_age.py",
    ),
    "scripts/run-affected.sh": (
        "tests/hermes_cli/test_run_affected_mapping.py",
        "tests/scripts/test_run_affected*.py",
    ),
    "setup.py": (
        "tests/test_packaging_build_guard.py",
        "tests/test_packaging_metadata.py",
    ),
    "tools/__init__.py": (
        "tests/tools/test_terminal_requirements.py",
        "tests/tools/test_terminal_tool_requirements.py",
    ),
    "tools/fal_common.py": (
        "tests/plugins/image_gen/test_fal_provider.py",
        "tests/plugins/video_gen/test_fal_plugin.py",
        "tests/tools/test_image_generation.py",
        "tests/tools/test_managed_media_gateways.py",
    ),
    "tools/feishu_doc_tool.py": ("tests/tools/test_feishu_tools.py",),
    "tools/feishu_drive_tool.py": ("tests/tools/test_feishu_tools.py",),
    "tools/neutts_synth.py": (
        "tests/tools/test_tts_provider_fallback.py",
        "tests/tools/test_voice_mode.py",
    ),
    "tools/xai_video_tools.py": (
        "tests/plugins/video_gen/test_xai_plugin.py",
        "tests/tools/test_video_generation_tool_surface_matrix.py",
    ),
}

# Feature packages whose tests live outside the mirrored source directory.
# These are real test mappings, not exceptions: touching any source in the
# feature selects its focused maintained suite.
FEATURE_PREFIX_TEST_PATTERNS: dict[str, tuple[str, ...]] = {
    # Skills and loop prompts are not Python, but they carry executable
    # instructions — a dead path or a stale interpreter in one of them misleads
    # every worker that reads it.  scripts/check_skill_hygiene.py is that gate
    # and its test runs the checker over the real tree.  Measured 2026-07-28:
    # without these two prefixes a SKILL.md or *-PROMPT.md change selected zero
    # test files, so the checker only ever ran when someone edited the checker
    # itself — the gate could not fail on the very change it polices.
    ".claude/skills/": ("tests/scripts/test_check_skill_hygiene.py",),
    "loops/packs/": ("tests/scripts/test_check_skill_hygiene.py",),
    "agent/pet/": (
        "tests/agent/test_pet*.py",
        "tests/cli/test_cli_pet_pane.py",
        "tests/hermes_cli/test_pet_toggle.py",
        "tests/tui_gateway/test_pet*.py",
    ),
    "bridges/discord_prefilter/": ("tests/discord_prefilter/test_*.py",),
    "gateway/platforms/qqbot/": ("tests/gateway/test_qqbot.py",),
    "hermes_cli/dashboard_auth/": (
        "tests/hermes_cli/test_dashboard_auth*.py",
        "tests/plugins/test_plugin_dashboard_auth_contract.py",
    ),
    "hermes_cli/proxy/": ("tests/hermes_cli/test_proxy.py",),
    "tools/computer_use/": (
        "tests/computer_use/test_*.py",
        "tests/tools/test_computer_use*.py",
    ),
    "tools/environments/": (
        "tests/tools/test_*environment.py",
        "tests/tools/test_base_environment.py",
    ),
}


class MappingError(RuntimeError):
    """Git, configuration or classification failure."""


class AffectedTestBudgetExceeded(MappingError):
    """The complete selected test set cannot fit its configured time budget."""

    def __init__(self, estimate: AffectedTestTimeEstimate) -> None:
        self.estimate = estimate
        super().__init__(estimate.fail_closed_message())


class GitTimeoutError(MappingError):
    """A git invocation exceeded the shared worktree timeout."""


@dataclass(frozen=True)
class ExceptionEntry:
    path: str
    disposition: str
    reason: str
    owner: str
    area: str
    review_by: str
    covered_by: str = ""


@dataclass(frozen=True)
class PathClassification:
    path: str
    state: str
    scope: str
    strategies: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str = ""
    exception: ExceptionEntry | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        if self.exception is None:
            payload.pop("exception")
        return payload


@dataclass(frozen=True)
class AffectedTestPlan:
    mode: str
    fallback_max_test_files: int
    records: tuple[PathClassification, ...]
    notes: tuple[str, ...] = ()

    @property
    def selected_tests(self) -> list[str]:
        return sorted({test for record in self.records for test in record.tests})

    @property
    def unmapped_paths(self) -> list[str]:
        return [record.path for record in self.records if record.state == "unmapped"]

    @property
    def counts(self) -> dict[str, int]:
        result = {
            "selected": 0,
            "not_applicable": 0,
            "allowlisted": 0,
            "unmapped": 0,
        }
        for record in self.records:
            result[record.state] += 1
        return result

    def to_dict(self) -> dict[str, object]:
        payload = {
            "mode": self.mode,
            "fallback_max_test_files": self.fallback_max_test_files,
            "counts": self.counts,
            "selected_tests": self.selected_tests,
            "records": [record.to_dict() for record in self.records],
        }
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


@dataclass(frozen=True)
class TestIndex:
    paths: tuple[str, ...]
    imports: Mapping[str, tuple[str, ...]]


def _normalize_path(raw: str) -> str:
    value = raw.replace("\\", "/").strip()
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise MappingError(f"invalid repository-relative path: {raw!r}")
    return path.as_posix()


def _run_git(repo_root: Path, *args: str) -> str:
    timeout = int(os.environ.get("HERMES_WORKTREE_GIT_TIMEOUT", "120"))
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError(
            f"git {' '.join(args[:3])}… timed out after {timeout}s in {repo_root}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise MappingError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout


def tracked_python_paths(repo_root: Path) -> list[str]:
    return sorted(
        {
            _normalize_path(line)
            for line in _run_git(
                repo_root,
                "ls-files",
                "--cached",
                "--",
                "*.py",
            ).splitlines()
            if line
        }
    )


def tracked_and_untracked_python_paths(repo_root: Path) -> list[str]:
    return sorted(
        {
            _normalize_path(line)
            for line in _run_git(
                repo_root,
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "*.py",
            ).splitlines()
            if line
        }
    )


def _default_diff_base(repo_root: Path) -> str:
    try:
        base = _run_git(repo_root, "merge-base", "HEAD", "main").strip()
    except GitTimeoutError:
        raise
    except MappingError:
        base = ""
    return base or "HEAD"


def _resolve_diff_spec(
    repo_root: Path,
    ref: str | None,
    right: str | None = None,
) -> SymbolDiffSpec:
    return SymbolDiffSpec(
        ref=ref,
        right=right,
        default_base=_default_diff_base(repo_root)
        if right is None and ref is None
        else None,
    )


def _changed_paths_for_spec(
    repo_root: Path,
    diff_spec: SymbolDiffSpec,
) -> list[str]:
    if diff_spec.right is not None:
        changed = {
            _normalize_path(line)
            for line in _run_git(
                repo_root,
                "diff",
                "--diff-filter=ACDMRT",
                "--name-only",
                diff_spec.ref or "HEAD",
                diff_spec.right,
            ).splitlines()
            if line
        }
    elif diff_spec.ref:
        changed = {
            _normalize_path(line)
            for line in _run_git(
                repo_root,
                "diff",
                "--diff-filter=ACDMRT",
                "--name-only",
                diff_spec.ref,
            ).splitlines()
            if line
        }
    else:
        base_ref = diff_spec.default_base or "HEAD"
        changed = {
            _normalize_path(line)
            for line in _run_git(
                repo_root,
                "diff",
                "--diff-filter=ACDMRT",
                "--name-only",
                base_ref,
            ).splitlines()
            if line
        }
        changed.update(
            _normalize_path(line)
            for line in _run_git(
                repo_root,
                "ls-files",
                "--others",
                "--exclude-standard",
            ).splitlines()
            if line
        )
    return sorted(changed)


def changed_paths_with_diff_spec(
    repo_root: Path,
    ref: str | None,
    right: str | None = None,
) -> tuple[list[str], SymbolDiffSpec]:
    """Return changed paths and the one resolved diff used to derive them."""

    diff_spec = _resolve_diff_spec(repo_root, ref, right)
    return _changed_paths_for_spec(repo_root, diff_spec), diff_spec


def changed_paths(
    repo_root: Path,
    ref: str | None,
    right: str | None = None,
) -> list[str]:
    paths, _ = changed_paths_with_diff_spec(repo_root, ref, right)
    return paths


def _is_test_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return parsed.name.startswith("test_") and "tests" in parsed.parts


def _is_under_tests(path: str) -> bool:
    return "tests" in PurePosixPath(path).parts


@lru_cache(maxsize=8192)
def _cached_test_imports(
    path_value: str,
    mtime_ns: int,
    size: int,
) -> frozenset[str]:
    del mtime_ns, size
    path = Path(path_value)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return frozenset()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
            imports.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
    return frozenset(imports)


def _test_imports(path: Path) -> set[str]:
    try:
        stat = path.stat()
    except OSError:
        return set()
    return set(
        _cached_test_imports(
            str(path.resolve()),
            stat.st_mtime_ns,
            stat.st_size,
        )
    )


def build_test_index(repo_root: Path) -> TestIndex:
    test_paths = [
        path
        for path in tracked_and_untracked_python_paths(repo_root)
        if _is_test_path(path)
        and not path.startswith("tests/stress/")
        and (repo_root / path).is_file()
    ]
    by_import: dict[str, set[str]] = {}
    for relative in test_paths:
        for module in _test_imports(repo_root / relative):
            by_import.setdefault(module, set()).add(relative)
    return TestIndex(
        paths=tuple(test_paths),
        imports={
            module: tuple(sorted(paths))
            for module, paths in sorted(by_import.items())
        },
    )


def _expand_patterns(repo_root: Path, patterns: Iterable[str]) -> list[str]:
    return sorted(
        {
            str(path.relative_to(repo_root))
            for pattern in patterns
            for path in repo_root.glob(pattern)
            if path.is_file()
        }
    )


def _explicit_tests(repo_root: Path, source_path: str) -> list[str]:
    patterns = list(EXPLICIT_TEST_PATTERNS.get(source_path, ()))
    for prefix, prefix_patterns in FEATURE_PREFIX_TEST_PATTERNS.items():
        if source_path.startswith(prefix):
            patterns.extend(prefix_patterns)
    return _expand_patterns(repo_root, patterns)


def _direct_tests(repo_root: Path, source_path: str) -> list[str]:
    source = PurePosixPath(source_path)
    name = f"test_{source.name}"
    candidates: set[PurePosixPath] = {PurePosixPath("tests") / source.parent / name}
    parent = source.parent
    while parent != PurePosixPath("."):
        candidates.add(parent / "tests" / name)
        parent = parent.parent
    return sorted(
        candidate.as_posix()
        for candidate in candidates
        if (repo_root / candidate).is_file()
    )


def _nearest_package_fallback(
    repo_root: Path,
    source_path: str,
    *,
    max_test_files: int,
) -> tuple[str | None, str | None]:
    parent = PurePosixPath(source_path).parent
    while parent != PurePosixPath("."):
        candidate = PurePosixPath("tests") / parent
        absolute = repo_root / candidate
        if absolute.is_dir():
            count = sum(1 for path in absolute.rglob("test_*.py") if path.is_file())
            if count <= max_test_files:
                return candidate.as_posix() + "/", None
            return None, (
                f"package fallback {candidate.as_posix()}/ has {count} test files; "
                f"mode limit is {max_test_files}"
            )
        parent = parent.parent
    return None, None


def _is_code_free_package_marker(repo_root: Path, source_path: str) -> bool:
    if not source_path.endswith("/__init__.py"):
        return False
    try:
        tree = ast.parse((repo_root / source_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return False
    body = list(tree.body)
    if not body:
        return True
    return (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )


def _is_deleted_production_path(repo_root: Path, source_path: str) -> bool:
    if (repo_root / source_path).is_file():
        return False
    return bool(
        _run_git(
            repo_root,
            "log",
            "-1",
            "--format=%H",
            "--",
            source_path,
        ).strip()
    )


def load_exceptions(
    repo_root: Path,
    exceptions_path: Path | None = None,
    *,
    today: date | None = None,
) -> tuple[dict[str, ExceptionEntry], dict[str, str]]:
    path = exceptions_path or repo_root / EXCEPTIONS_PATH
    if not path.is_file():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MappingError(f"cannot read affected-test exceptions: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("exceptions"), list
    ):
        raise MappingError("affected-test exceptions require schema_version=1 and a list")

    current = today or date.today()
    result: dict[str, ExceptionEntry] = {}
    rejected: dict[str, str] = {}
    seen: set[str] = set()
    required = {"path", "disposition", "reason", "owner", "area", "review_by"}
    for raw in payload["exceptions"]:
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise MappingError(
                "each affected-test exception requires path, disposition, reason, "
                "owner, area and review_by"
            )
        normalized = _normalize_path(str(raw["path"]))
        if any(char in normalized for char in "*?[]"):
            raise MappingError(f"affected-test exception path must be exact: {normalized}")
        if normalized in seen:
            raise MappingError(f"duplicate affected-test exception: {normalized}")
        seen.add(normalized)
        disposition = str(raw["disposition"]).strip()
        if disposition not in _EXCEPTION_DISPOSITIONS:
            raise MappingError(
                f"invalid affected-test exception disposition for {normalized}: "
                f"{disposition}"
            )
        try:
            review_by = date.fromisoformat(str(raw["review_by"]))
        except ValueError as exc:
            raise MappingError(
                f"invalid affected-test exception review_by for {normalized}"
            ) from exc
        covered_by = str(raw.get("covered_by", "")).strip()
        if disposition == "covered_by_other_gate" and not covered_by:
            raise MappingError(
                f"covered_by is required for affected-test exception {normalized}"
            )
        entry = ExceptionEntry(
            path=normalized,
            disposition=disposition,
            reason=str(raw["reason"]).strip(),
            owner=str(raw["owner"]).strip(),
            area=str(raw["area"]).strip(),
            review_by=review_by.isoformat(),
            covered_by=covered_by,
        )
        if not entry.reason or not entry.owner or not entry.area:
            raise MappingError(
                f"blank affected-test exception metadata for {normalized}"
            )
        if not (repo_root / normalized).is_file():
            rejected[normalized] = (
                "stale affected-test exception ignored; path is not a file"
            )
            continue
        if review_by < current:
            rejected[normalized] = (
                f"expired affected-test exception ignored; review_by={review_by.isoformat()}"
            )
            continue
        result[normalized] = entry
    return result, rejected


def _classify_changed_paths(
    repo_root: Path,
    paths: Sequence[str],
    *,
    mode: str = "integration",
    exceptions_path: Path | None = None,
    index: TestIndex | None = None,
    diff_spec: SymbolDiffSpec | None = None,
    enforce_time_budget: bool,
) -> AffectedTestPlan:
    if mode not in {"worker", "integration"}:
        raise MappingError(f"unknown affected-test mode: {mode}")
    fallback_limit = (
        WORKER_FALLBACK_MAX_TEST_FILES
        if mode == "worker"
        else INTEGRATION_FALLBACK_MAX_TEST_FILES
    )
    test_index = index
    exceptions, rejected_exceptions = load_exceptions(repo_root, exceptions_path)
    records: list[PathClassification] = []

    for raw_path in sorted(set(paths)):
        source_path = _normalize_path(raw_path)
        explicit = _explicit_tests(repo_root, source_path)
        rejected_warning = rejected_exceptions.get(source_path)
        if explicit and not source_path.endswith(".py"):
            if source_path in exceptions:
                raise MappingError(
                    f"mapped path must not remain allowlisted: {source_path}"
                )
            records.append(
                PathClassification(
                    path=source_path,
                    state="selected",
                    scope="gate_control"
                    if not source_path.endswith(".py")
                    else "production_python",
                    strategies=("explicit",),
                    tests=tuple(explicit),
                    warnings=(rejected_warning,) if rejected_warning else (),
                )
            )
            continue

        if not source_path.endswith(".py"):
            records.append(
                PathClassification(
                    path=source_path,
                    state="not_applicable",
                    scope="non_python",
                    warnings=(rejected_warning,) if rejected_warning else (),
                    reason="no Python production path changed",
                )
            )
            continue

        if _is_test_path(source_path):
            if source_path.startswith("tests/stress/"):
                records.append(
                    PathClassification(
                        path=source_path,
                        state="not_applicable",
                        scope="stress_scenario",
                        warnings=(rejected_warning,) if rejected_warning else (),
                        reason="stress scenarios use their own registry, not pytest",
                    )
                )
            elif (repo_root / source_path).is_file():
                records.append(
                    PathClassification(
                        path=source_path,
                        state="selected",
                        scope="test",
                        strategies=("changed_test",),
                        tests=(source_path,),
                        warnings=(rejected_warning,) if rejected_warning else (),
                    )
                )
            else:
                records.append(
                    PathClassification(
                        path=source_path,
                        state="not_applicable",
                        scope="deleted_test",
                        warnings=(rejected_warning,) if rejected_warning else (),
                        reason="deleted tests cannot be executed",
                    )
                )
            continue

        if _is_under_tests(source_path):
            records.append(
                PathClassification(
                    path=source_path,
                    state="not_applicable",
                    scope="test_support",
                    warnings=(rejected_warning,) if rejected_warning else (),
                    reason=(
                        "Python support files under tests are outside the "
                        "affected production-path contract"
                    ),
                )
            )
            continue

        source_deleted = _is_deleted_production_path(repo_root, source_path)
        source = PurePosixPath(source_path)
        if source.name == "__init__.py":
            module_import = source.parent.as_posix().replace("/", ".")
        else:
            module_import = source_path[:-3].replace("/", ".")
        strategies: list[str] = ["explicit"] if explicit else []
        direct = _direct_tests(repo_root, source_path)
        if direct:
            strategies.append("direct")
        if test_index is None:
            test_index = build_test_index(repo_root)
        imported = test_index.imports.get(module_import, ())
        uncovered_symbols: tuple[str, ...] = ()
        uncovered_without_net = False
        if imported:
            broad_imported = imported
            narrowing = narrow_imported_tests(
                repo_root=repo_root,
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
            imported = narrowing.tests
            if narrowing.reason == "no_symbol_test_matches":
                uncovered_symbols = narrowing.changed_symbols
                # Dropping the module-level set on an untested symbol is only safe
                # where a curated or direct test still covers the path. Measured
                # 2026-07-28: of the 14 modules above the fan-out threshold, 6 have
                # neither (gateway/run.py, gateway/platforms/base.py, run_agent.py,
                # cli.py, hermes_cli/main.py, hermes_cli/auth.py) — zeroing there
                # would select no test at all and still report green.
                if not direct and not explicit:
                    imported = broad_imported
                    uncovered_without_net = True
            narrowed = narrowing.applied and not uncovered_without_net
            strategies.append("import→symbol" if narrowed else "import")
        prioritized_tests: list[str] = []
        seen_tests: set[str] = set()
        for evidence in (direct, explicit, imported):
            for test in sorted(evidence):
                if test not in seen_tests:
                    seen_tests.add(test)
                    prioritized_tests.append(test)
        selected_warnings = (
            [rejected_warning] if rejected_warning is not None else []
        )
        if uncovered_symbols:
            symbol_label = "symbol" if len(uncovered_symbols) == 1 else "symbols"
            if uncovered_without_net:
                coverage_note = (
                    "no curated or direct test covers this path, so all "
                    f"{len(prioritized_tests)} module-level import matches still run"
                )
            else:
                coverage_note = (
                    f"the {len(prioritized_tests)} curated/direct test files for "
                    "this path ran instead of the module-level import set"
                )
            selected_warnings.append(
                f"symbol coverage gap for {source_path}: changed {symbol_label} "
                f"without test references: {', '.join(uncovered_symbols)}; "
                f"{coverage_note} and the affected-test gate intentionally "
                "remains non-red"
            )
        if prioritized_tests or uncovered_symbols:
            if source_path in exceptions:
                raise MappingError(
                    f"mapped path must not remain allowlisted: {source_path}"
                )
            records.append(
                PathClassification(
                    path=source_path,
                    state="selected",
                    scope="production_python",
                    strategies=tuple(strategies),
                    tests=tuple(sorted(prioritized_tests)),
                    warnings=tuple(selected_warnings),
                )
            )
            continue

        if source_deleted:
            records.append(
                PathClassification(
                    path=source_path,
                    state="not_applicable",
                    scope="deleted_production",
                    warnings=(rejected_warning,) if rejected_warning else (),
                    reason="deleted production path has no surviving direct or importing test",
                )
            )
            continue

        if source_path.startswith(_NOT_APPLICABLE_PREFIXES):
            records.append(
                PathClassification(
                    path=source_path,
                    state="not_applicable",
                    scope="catalog_or_docs",
                    warnings=(rejected_warning,) if rejected_warning else (),
                    reason="outside the Hermes core pytest gate",
                )
            )
            continue

        if _is_code_free_package_marker(repo_root, source_path):
            records.append(
                PathClassification(
                    path=source_path,
                    state="not_applicable",
                    scope="package_marker",
                    warnings=(rejected_warning,) if rejected_warning else (),
                    reason="empty or docstring-only __init__.py",
                )
            )
            continue

        exception = exceptions.get(source_path)
        if exception is not None:
            records.append(
                PathClassification(
                    path=source_path,
                    state="allowlisted",
                    scope="production_python",
                    reason=exception.reason,
                    exception=exception,
                )
            )
            continue

        fallback, fallback_warning = _nearest_package_fallback(
            repo_root,
            source_path,
            max_test_files=fallback_limit,
        )
        if fallback:
            records.append(
                PathClassification(
                    path=source_path,
                    state="selected",
                    scope="production_python",
                    strategies=("package_fallback",),
                    tests=(fallback,),
                    warnings=(rejected_warning,) if rejected_warning else (),
                )
            )
            continue

        warnings = tuple(
            warning
            for warning in (fallback_warning, rejected_warning)
            if warning
        )
        records.append(
            PathClassification(
                path=source_path,
                state="unmapped",
                scope="production_python",
                warnings=warnings,
                reason="no selected test and no audited exception",
            )
        )

    plan = AffectedTestPlan(
        mode=mode,
        fallback_max_test_files=fallback_limit,
        records=tuple(records),
    )
    if not enforce_time_budget:
        return plan
    try:
        budget_check = check_affected_test_budget(repo_root, plan.selected_tests)
    except AffectedTestBudgetConfigError as exc:
        raise MappingError(str(exc)) from exc
    if budget_check.note:
        plan = replace(plan, notes=(budget_check.note,))
    if (
        budget_check.estimate is not None
        and budget_check.estimate.exceeds_budget
    ):
        raise AffectedTestBudgetExceeded(budget_check.estimate)
    return plan


def classify_changed_paths(
    repo_root: Path,
    paths: Sequence[str],
    *,
    mode: str = "integration",
    exceptions_path: Path | None = None,
    index: TestIndex | None = None,
    diff_spec: SymbolDiffSpec | None = None,
) -> AffectedTestPlan:
    return _classify_changed_paths(
        repo_root,
        paths,
        mode=mode,
        exceptions_path=exceptions_path,
        index=index,
        diff_spec=diff_spec,
        enforce_time_budget=True,
    )


def affected_pytest_modules(
    repo_root: Path,
    changed_files: Sequence[str],
    *,
    mode: str = "integration",
) -> list[str]:
    """Compatibility wrapper that preserves the classifier's fail-closed state."""
    plan = classify_changed_paths(repo_root, changed_files, mode=mode)
    if plan.unmapped_paths:
        raise MappingError(
            "unmapped production paths: " + ", ".join(plan.unmapped_paths)
        )
    for note in plan.notes:
        warnings.warn(note, RuntimeWarning, stacklevel=2)
    return plan.selected_tests


def census_repository(
    repo_root: Path,
    *,
    mode: str,
    exceptions_path: Path | None = None,
) -> AffectedTestPlan:
    sources = [
        path
        for path in tracked_python_paths(repo_root)
        if not _is_under_tests(path) and (repo_root / path).is_file()
    ]
    return _classify_changed_paths(
        repo_root,
        sources,
        mode=mode,
        exceptions_path=exceptions_path,
        # Census classifies inventory but never executes its aggregate test set.
        enforce_time_budget=False,
    )
