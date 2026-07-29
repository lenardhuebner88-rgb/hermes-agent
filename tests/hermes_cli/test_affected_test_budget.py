from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.affected_test_budget import (
    AFFECTED_TIME_BUDGET_ENV,
    AffectedTestBudgetConfigError,
    AffectedTestTimeEstimate,
    LOADED_HOST_DILATION,
    UNKNOWN_TEST_DURATION_SECONDS,
    check_affected_test_budget,
    load_test_durations,
)


# Verbatim values from the 2026-07-28 operational test_durations.json.  The
# absolute probe entry is real junk produced by a subprocess probe and must
# never become a repository test forecast.
REAL_DURATION_FRAGMENT = {
    "/tmp/cg_probe_fail.py": 0.684,
    "/tmp/cg_probe_ok.py": 0.681,
    "tests/hermes_cli/test_web_server.py": 96.902,
    "tests/gateway/test_config.py": 24.236,
    "tests/cron/test_scheduler.py": 23.574,
}


def _write_cache(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_real_duration_fragment_filters_absolute_junk_and_counts_unknown(
    tmp_path: Path,
) -> None:
    cache = _write_cache(tmp_path / "durations.json", REAL_DURATION_FRAGMENT)

    durations, error = load_test_durations(cache)
    check = check_affected_test_budget(
        tmp_path,
        [
            "tests/hermes_cli/test_web_server.py",
            "tests/gateway/test_config.py",
            "tests/new/test_without_forecast.py",
        ],
        durations_path=cache,
        env={AFFECTED_TIME_BUDGET_ENV: "1200", "HERMES_TEST_WORKERS": "2"},
    )

    assert error == ""
    assert durations == {
        "tests/hermes_cli/test_web_server.py": 96.902,
        "tests/gateway/test_config.py": 24.236,
        "tests/cron/test_scheduler.py": 23.574,
    }
    assert check.estimate is not None
    assert check.estimate.file_count == 3
    assert check.estimate.missing_forecast_count == 1
    assert check.estimate.predicted_seconds == pytest.approx(
        96.902 * LOADED_HOST_DILATION
    )
    assert all(not item.path.startswith("/") for item in check.estimate.top_files)


@pytest.mark.parametrize(
    ("cache_name", "contents", "reason"),
    [
        ("missing.json", None, "missing"),
        ("broken.json", "{definitely not JSON", "JSONDecodeError"),
    ],
)
def test_missing_or_corrupt_cache_skips_budget_check_visibly(
    tmp_path: Path,
    cache_name: str,
    contents: str | None,
    reason: str,
) -> None:
    cache = tmp_path / cache_name
    if contents is not None:
        cache.write_text(contents, encoding="utf-8")

    check = check_affected_test_budget(
        tmp_path,
        ["tests/test_selected.py"],
        durations_path=cache,
        env={AFFECTED_TIME_BUDGET_ENV: "1"},
    )

    assert check.estimate is None
    assert "time-budget check skipped" in check.note
    assert reason in check.note
    assert "complete selection retained (1 file)" in check.note


def test_over_budget_message_names_complete_actionable_forecast(
    tmp_path: Path,
) -> None:
    selected = [f"tests/test_{index}.py" for index in range(6)]
    cache = _write_cache(
        tmp_path / "durations.json",
        {
            selected[0]: 400.0,
            selected[1]: 300.0,
            selected[2]: 200.0,
            selected[3]: 100.0,
            selected[4]: 50.0,
        },
    )

    check = check_affected_test_budget(
        tmp_path,
        selected,
        durations_path=cache,
        env={AFFECTED_TIME_BUDGET_ENV: "100", "HERMES_TEST_WORKERS": "8"},
    )

    assert check.estimate is not None
    assert check.estimate.exceeds_budget
    message = check.estimate.fail_closed_message()
    assert "predicted loaded wall time 1400.0s" in message
    assert "budget 100.0s" in message
    assert "6 selected test files" in message
    assert "1 file without duration forecast" in message
    assert "top-5 estimated files" in message
    assert "tests/test_0.py=400.0s" in message
    assert "tests/test_5.py=60.0s (assumed)" in message
    assert "tests/test_4.py" not in message
    assert f"uses {UNKNOWN_TEST_DURATION_SECONDS:.1f}s each" in message
    assert "8 workers" in message
    assert "3.5 loaded-host dilation" in message


def test_under_budget_selection_keeps_every_file(tmp_path: Path) -> None:
    selected = [f"tests/test_{index}.py" for index in range(3)]
    cache = _write_cache(
        tmp_path / "durations.json",
        {path: 1.0 for path in selected},
    )

    check = check_affected_test_budget(
        tmp_path,
        selected,
        durations_path=cache,
        env={AFFECTED_TIME_BUDGET_ENV: "1200", "HERMES_TEST_WORKERS": "8"},
    )

    assert check.estimate is not None
    assert not check.estimate.exceeds_budget
    assert check.estimate.file_count == len(selected)
    assert {item.path for item in check.estimate.top_files} == set(selected)


def test_invalid_budget_configuration_is_not_silently_ignored(tmp_path: Path) -> None:
    cache = _write_cache(
        tmp_path / "durations.json",
        {"tests/test_selected.py": 1.0},
    )

    with pytest.raises(
        AffectedTestBudgetConfigError,
        match="must be a positive finite number",
    ):
        check_affected_test_budget(
            tmp_path,
            ["tests/test_selected.py"],
            durations_path=cache,
            env={AFFECTED_TIME_BUDGET_ENV: "not-a-number"},
        )


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_exceeds_budget_false_when_equal():
    """Kill comparison_swap L58 (> -> >=): mutant returns True when equal."""
    est = AffectedTestTimeEstimate(
        predicted_seconds=100.0,
        budget_seconds=100.0,
        serial_seconds=100.0,
        slowest_seconds=100.0,
        file_count=1,
        missing_forecast_count=0,
        workers=1,
        top_files=(),
    )
    # Original: 100 > 100 -> False
    # Mutant (>=): 100 >= 100 -> True
    assert est.exceeds_budget is False


def test_zero_duration_included_in_forecast(tmp_path: Path):
    """Kill comparison_swap L122 (>= -> >): mutant excludes duration=0."""
    cache = _write_cache(
        tmp_path / "durations.json",
        {"tests/test_zero.py": 0.0, "tests/test_one.py": 1.0},
    )
    durations, error = load_test_durations(cache)
    assert error == ""
    # Original: 0.0 >= 0 -> True -> included
    # Mutant (>): 0.0 > 0 -> False -> excluded
    assert "tests/test_zero.py" in durations
    assert durations["tests/test_zero.py"] == 0.0


def test_env_budget_zero_raises(tmp_path: Path):
    """Kill comparison_swap L141 (<= -> <): mutant accepts value=0."""
    cache = _write_cache(tmp_path / "durations.json", {"tests/test_a.py": 1.0})
    # Original: 0 <= 0 -> True -> raises
    # Mutant (<): 0 < 0 -> False -> does NOT raise
    with pytest.raises(AffectedTestBudgetConfigError, match="positive finite"):
        check_affected_test_budget(
            tmp_path,
            ["tests/test_a.py"],
            durations_path=cache,
            env={AFFECTED_TIME_BUDGET_ENV: "0"},
        )
