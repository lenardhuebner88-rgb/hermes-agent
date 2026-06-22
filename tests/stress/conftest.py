"""pytest config for the stress/ subdirectory.

These tests are slow (30s+), spawn subprocesses, and are not run by
default. Enable via `pytest --run-stress` or by running the scripts
directly.

Two categories live here:

1. ``__main__``-executable scripts (no top-level ``def test_``) — these
   are listed in ``_SCRIPT_FILES`` and excluded from pytest collection
   so they don't cause import-time side effects or empty-suite noise.
2. Proper pytest test modules (``test_*.py`` with ``def test_``) — these
   ARE collected, but skipped unless ``--run-stress`` is passed.
"""
import pytest

# Files that are scripts, not pytest-collectable test modules.
# They have no top-level ``def test_`` and are meant to be run via
# ``python tests/stress/<name>.py``.
_SCRIPT_FILES = {
    "test_concurrency.py",
    "test_concurrency_parent_gate.py",
    "test_concurrency_mixed.py",
    "test_concurrency_reclaim_race.py",
    "test_benchmarks.py",
    "test_atypical_scenarios.py",
    "test_subprocess_e2e.py",
    "test_property_fuzzing.py",
}

# Build collect_ignore_glob dynamically: always ignore scripts,
# and ignore proper test modules unless --run-stress is active.
# (When --run-stress IS active, test modules are collected and the
# pytest_collection_modifyitems hook below does NOT skip them.)
collect_ignore_glob = list(_SCRIPT_FILES)


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-stress", default=False):
        return
    skip_stress = pytest.mark.skip(
        reason="stress test (opt-in via --run-stress or run script directly)"
    )
    for item in items:
        if "tests/stress" in str(item.fspath):
            item.add_marker(skip_stress)


def pytest_addoption(parser):
    parser.addoption(
        "--run-stress",
        action="store_true",
        default=False,
        help="Run the stress/battle-test suite (slow, spawns subprocesses).",
    )
