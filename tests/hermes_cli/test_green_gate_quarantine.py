"""Fail-closed exact-name policy for nightly green-gate quarantine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.green_gate_quarantine import (
    QuarantinePolicyError,
    classify_failures,
    gate_file_evidence_complete,
    load_quarantine_policy,
)


def _policy(tmp_path: Path, entries: list[str]) -> Path:
    path = tmp_path / "green_gate_quarantine_allowlist.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_exact_named_failure_is_environment_only(tmp_path):
    policy = load_quarantine_policy(_policy(tmp_path, ["tests/env/test_clock.py"]))

    result = classify_failures(
        ["tests/env/test_clock.py"],
        unattributed_fail_gates=[],
        policy=policy,
    )

    assert result["failure_classification"] == "environment_quarantine"
    assert result["failed_test_files"] == ["tests/env/test_clock.py"]
    assert result["quarantined_test_files"] == ["tests/env/test_clock.py"]
    assert result["blocking_test_files"] == []
    assert result["unattributed_fail_gates"] == []


def test_mixed_quarantined_and_real_failure_is_regression(tmp_path):
    policy = load_quarantine_policy(_policy(tmp_path, ["tests/env/test_clock.py"]))

    result = classify_failures(
        ["tests/env/test_clock.py", "tests/product/test_regression.py"],
        unattributed_fail_gates=[],
        policy=policy,
    )

    assert result["failure_classification"] == "regression"
    assert result["quarantined_test_files"] == ["tests/env/test_clock.py"]
    assert result["blocking_test_files"] == ["tests/product/test_regression.py"]


@pytest.mark.parametrize(
    "entry",
    [
        "tests/**/test_clock.py",
        "tests/env/test_*.py",
        "../tests/env/test_clock.py",
        "/tests/env/test_clock.py",
        "tests\\env\\test_clock.py",
        "tests/env/`injected`.py",
        "tests/env/test_clock.py\nextra",
        "",
    ],
)
def test_policy_rejects_patterns_traversal_and_noncanonical_names(tmp_path, entry):
    with pytest.raises(QuarantinePolicyError):
        load_quarantine_policy(_policy(tmp_path, [entry]))


def test_missing_policy_is_safe_empty(tmp_path):
    policy = load_quarantine_policy(tmp_path / "missing.json")
    result = classify_failures(
        ["tests/env/test_clock.py"],
        unattributed_fail_gates=[],
        policy=policy,
    )

    assert policy.entries == ()
    assert result["failure_classification"] == "regression"
    assert result["blocking_test_files"] == ["tests/env/test_clock.py"]


def test_unattributed_gate_can_never_be_quarantined(tmp_path):
    policy = load_quarantine_policy(_policy(tmp_path, ["tests/env/test_clock.py"]))
    result = classify_failures(
        ["tests/env/test_clock.py"],
        unattributed_fail_gates=["tsc"],
        policy=policy,
    )

    assert result["failure_classification"] == "regression"
    assert result["unattributed_fail_gates"] == ["tsc"]


def test_python_file_evidence_requires_matching_summary_count():
    complete = (
        "=== 1 file with test failures (1 test failed) ===\n"
        "  tests/env/test_clock.py  (1 test failed)\n"
    )
    incomplete = complete.replace("=== 1 file", "=== 2 files")

    assert gate_file_evidence_complete("python", complete, ["tests/env/test_clock.py"])
    assert not gate_file_evidence_complete(
        "python", incomplete, ["tests/env/test_clock.py"]
    )


def test_python_extra_setup_error_keeps_otherwise_matching_files_unattributed():
    log = (
        "=== 1 file with test failures (1 test failed) ===\n"
        "  tests/env/test_clock.py  (1 test failed)\n"
        "=== 1 file with pytest setup/teardown/collection errors (1 error) ===\n"
        "  tests/product/test_fixture.py  (1 error, 0 passed)\n"
    )

    assert not gate_file_evidence_complete("python", log, ["tests/env/test_clock.py"])


def test_vitest_unhandled_error_keeps_file_evidence_unattributed():
    log = (
        " FAIL  src/env.test.ts > suite > case\n"
        " Test Files  1 failed (1)\n"
        " Unhandled Error\n"
    )

    assert not gate_file_evidence_complete("vitest", log, ["src/env.test.ts"])


def test_heartbeat_wires_classification_fields_into_ledger_command():
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "green-gate-heartbeat.sh"
    ).read_text(encoding="utf-8")

    assert "hermes vision classify-gate-failures" in script
    assert '--failure-classification "$failure_classification"' in script
    assert '--failed-test-files-json "$failed_test_files_json"' in script
    assert '--blocking-test-files-json "$blocking_test_files_json"' in script
    assert (
        "Collect classification evidence for EVERY red gate before isolation" in script
    )
    assert 'fallback_args=(fail --first-fail-gate "$first_fail_gate"' in script
    assert '--unattributed-gate "$classification_gate"' in script
    assert script.index('for classification_gate in "${fail_gates[@]}"') < script.index(
        'for g in "${fail_gates[@]}"'
    )
