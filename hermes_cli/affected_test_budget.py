"""Runtime-budget estimation for complete affected-test selections.

The duration cache is an operational hint, not repository truth.  A readable
cache keeps unknown selected tests explicit by assigning them a conservative
duration; unavailable caches fail closed unless explicitly overridden.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


AFFECTED_TIME_BUDGET_ENV = "HERMES_AFFECTED_TIME_BUDGET"
AFFECTED_BUDGET_OVERRIDE_ENV = "HERMES_AFFECTED_BUDGET_OK"
DEFAULT_AFFECTED_TIME_BUDGET_SECONDS = 1200.0
DEFAULT_TEST_WORKERS = 8
LOADED_HOST_DILATION = 3.5
UNKNOWN_TEST_DURATION_SECONDS = 60.0
TEST_DURATIONS_FILENAME = "test_durations.json"
TOP_DURATION_COUNT = 5


class AffectedTestBudgetConfigError(ValueError):
    """The configured affected-test budget is unusable."""


@dataclass(frozen=True)
class EstimatedTestFile:
    path: str
    seconds: float
    forecast_available: bool


@dataclass(frozen=True)
class AffectedTestTimeEstimate:
    """Loaded-host wall-time estimate for one complete selected test set.

    Cache values are interpreted as serial per-file subprocess seconds.  The
    ideal parallel floor is the larger of serial_total/workers and the slowest
    file; multiplying that floor by 3.5 accounts for the measured 3.3-3.6x
    dilation when dashboard and agent sessions share the host.
    """

    predicted_seconds: float
    budget_seconds: float
    serial_seconds: float
    slowest_seconds: float
    file_count: int
    missing_forecast_count: int
    workers: int
    top_files: tuple[EstimatedTestFile, ...]

    @property
    def exceeds_budget(self) -> bool:
        return self.predicted_seconds > self.budget_seconds

    def fail_closed_message(self) -> str:
        selected_noun = "file" if self.file_count == 1 else "files"
        missing_noun = (
            "file" if self.missing_forecast_count == 1 else "files"
        )
        missing_verb = "uses" if self.missing_forecast_count == 1 else "use"
        top = ", ".join(
            f"{item.path}={item.seconds:.1f}s"
            + ("" if item.forecast_available else " (assumed)")
            for item in self.top_files
        )
        return (
            "affected-test time budget exceeded: predicted loaded wall time "
            f"{self.predicted_seconds:.1f}s exceeds budget "
            f"{self.budget_seconds:.1f}s for {self.file_count} selected test "
            f"{selected_noun}; {self.missing_forecast_count} {missing_noun} "
            "without duration "
            f"forecast {missing_verb} "
            f"{UNKNOWN_TEST_DURATION_SECONDS:.1f}s each; "
            f"top-{len(self.top_files)} estimated files: {top}; "
            "model=max(serial "
            f"{self.serial_seconds:.1f}s / {self.workers} workers, slowest file "
            f"{self.slowest_seconds:.1f}s) x {LOADED_HOST_DILATION:.1f} "
            "loaded-host dilation"
        )


@dataclass(frozen=True)
class AffectedTestBudgetCheck:
    estimate: AffectedTestTimeEstimate | None
    note: str = ""


def _is_repo_relative_test_path(raw: object) -> bool:
    if not isinstance(raw, str) or not raw.startswith("tests/"):
        return False
    parsed = PurePosixPath(raw)
    return (
        not parsed.is_absolute()
        and ".." not in parsed.parts
        and parsed.as_posix() == raw
    )


def load_test_durations(path: Path) -> tuple[dict[str, float] | None, str]:
    """Load only finite, non-negative, repository-relative ``tests/`` entries."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"duration cache missing at {path}"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"duration cache unreadable at {path}: {type(exc).__name__}"
    if not isinstance(raw, dict):
        return None, f"duration cache at {path} is not a JSON object"

    durations: dict[str, float] = {}
    for key, value in raw.items():
        if not _is_repo_relative_test_path(key):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            durations[key] = seconds
    return durations, ""


def _main_checkout_duration_cache(
    repo_root: Path,
) -> tuple[Path | None, str]:
    command = [
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, (
            f"main-checkout duration cache path unresolved from {repo_root}: "
            f"git rev-parse failed with {type(exc).__name__}"
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        return None, (
            f"main-checkout duration cache path unresolved from {repo_root}: "
            f"git rev-parse exited {completed.returncode}{suffix}"
        )
    common_dir = (completed.stdout or "").strip()
    if not common_dir:
        return None, (
            f"main-checkout duration cache path unresolved from {repo_root}: "
            "git rev-parse returned an empty git common directory"
        )
    try:
        common_path = Path(common_dir).resolve()
    except (OSError, RuntimeError) as exc:
        return None, (
            f"main-checkout duration cache path unresolved from {repo_root}: "
            f"{type(exc).__name__}"
        )
    main_checkout = (
        common_path.parent if common_path.name == ".git" else common_path
    )
    return main_checkout / TEST_DURATIONS_FILENAME, ""


def _unavailable_cache_message(errors: Sequence[str]) -> str:
    return (
        "affected-test time-budget check unavailable: "
        + "; ".join(errors)
        + "; no test selection will run without a runtime estimate. "
        "Fix: provide a readable test_durations.json at one of the paths "
        "above; for a deliberate no-cache override use "
        f"{AFFECTED_BUDGET_OVERRIDE_ENV}=1"
    )


def _positive_finite_env_seconds(
    env: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise AffectedTestBudgetConfigError(
            f"{name} must be a positive finite number of seconds, got {raw!r}"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise AffectedTestBudgetConfigError(
            f"{name} must be a positive finite number of seconds, got {raw!r}"
        )
    return value


def _test_workers(env: Mapping[str, str]) -> int:
    raw = env.get("HERMES_TEST_WORKERS", "")
    try:
        workers = int(raw)
    except ValueError:
        return DEFAULT_TEST_WORKERS
    return workers if workers > 0 else DEFAULT_TEST_WORKERS


def _selected_test_files(
    repo_root: Path,
    selected_targets: Sequence[str],
) -> list[str]:
    files: set[str] = set()
    for target in selected_targets:
        candidate = repo_root / target
        if target.endswith("/") or candidate.is_dir():
            files.update(
                path.relative_to(repo_root).as_posix()
                for path in candidate.rglob("test_*.py")
                if path.is_file()
            )
        else:
            files.add(target)
    return sorted(files)


def check_affected_test_budget(
    repo_root: Path,
    selected_targets: Sequence[str],
    *,
    durations_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AffectedTestBudgetCheck:
    """Estimate a complete selection, failing closed when no cache is usable."""
    selected_files = _selected_test_files(repo_root, selected_targets)
    if not selected_files:
        return AffectedTestBudgetCheck(estimate=None)

    active_env = os.environ if env is None else env
    cache_errors: list[str] = []
    cache_path = durations_path or repo_root / TEST_DURATIONS_FILENAME
    durations, cache_error = load_test_durations(cache_path)
    if (
        durations is None
        and durations_path is None
        and cache_error == f"duration cache missing at {cache_path}"
    ):
        cache_errors.append(cache_error)
        main_cache_path, resolution_error = _main_checkout_duration_cache(
            repo_root
        )
        if resolution_error:
            cache_errors.append(resolution_error)
        elif main_cache_path is not None and (
            main_cache_path.resolve() != cache_path.resolve()
        ):
            cache_path = main_cache_path
            durations, cache_error = load_test_durations(cache_path)
    if durations is None:
        if cache_error not in cache_errors:
            cache_errors.append(cache_error)
        selected_noun = "file" if len(selected_files) == 1 else "files"
        unavailable_message = _unavailable_cache_message(cache_errors)
        if active_env.get(AFFECTED_BUDGET_OVERRIDE_ENV) != "1":
            raise AffectedTestBudgetConfigError(unavailable_message)
        return AffectedTestBudgetCheck(
            estimate=None,
            note=(
                "affected-test time-budget check skipped via deliberate "
                f"{AFFECTED_BUDGET_OVERRIDE_ENV}=1 override: "
                f"{'; '.join(cache_errors)}; complete selection retained "
                f"({len(selected_files)} {selected_noun})"
            ),
        )

    budget_seconds = _positive_finite_env_seconds(
        active_env,
        AFFECTED_TIME_BUDGET_ENV,
        DEFAULT_AFFECTED_TIME_BUDGET_SECONDS,
    )
    workers = _test_workers(active_env)
    estimated_files = [
        EstimatedTestFile(
            path=path,
            seconds=durations.get(path, UNKNOWN_TEST_DURATION_SECONDS),
            forecast_available=path in durations,
        )
        for path in selected_files
    ]
    serial_seconds = sum(item.seconds for item in estimated_files)
    slowest_seconds = max(item.seconds for item in estimated_files)
    predicted_seconds = (
        max(serial_seconds / workers, slowest_seconds) * LOADED_HOST_DILATION
    )
    top_files = tuple(
        sorted(
            estimated_files,
            key=lambda item: (-item.seconds, item.path),
        )[:TOP_DURATION_COUNT]
    )
    estimate = AffectedTestTimeEstimate(
        predicted_seconds=predicted_seconds,
        budget_seconds=budget_seconds,
        serial_seconds=serial_seconds,
        slowest_seconds=slowest_seconds,
        file_count=len(estimated_files),
        missing_forecast_count=sum(
            not item.forecast_available for item in estimated_files
        ),
        workers=workers,
        top_files=top_files,
    )
    return AffectedTestBudgetCheck(estimate=estimate)
