"""Fork-side test provisioning for upstream plugin tests.

Jobs:

1. Let upstream's own ``test_kanban_model_override.py`` run against the fork
   instead of dying in its ``_create`` helper.
2. Provide a module-scoped HERMES_HOME template (initialized kanban DB +
   profile directories) so ``test_kanban_dashboard_plugin.py`` can copy
   instead of re-creating per test — the mechanism that keeps 310 tests
   inside the Nightly's 300 s per-file budget under ``-j 12`` contention.

Upstream builds its tasks with ``assignee="worker"``. The fork validates the
assignee against the on-disk Hermes profiles
(``kanban_db.validate_spawnable_assignee`` -> ``profiles.profile_exists`` ->
``get_profile_dir(name).is_dir()``) and rejects unknown ones with
"Assignee 'worker' is not spawnable: no on-disk Hermes profile". Upstream has
no such guard, so all five of its override tests failed at the *create* step
without ever reaching the behaviour they assert.

Two ways to make them green were rejected:

* editing upstream's test file — that adds exactly the merge burden the
  upstream-capability workstream exists to remove
  (docs/refactor/UPSTREAM-STRATEGY.md);
* monkeypatching ``profile_exists`` to ``True`` — that deletes the fork's
  guard from the run, so the tests would pass without the guard ever being
  exercised. A green test that skipped a real code path is worse than a red
  one.

So instead the missing precondition is *satisfied*: a real profile directory
is created inside the test's own temporary ``HERMES_HOME``. The fork's guard
then executes for real and legitimately succeeds. Nothing is patched away and
no upstream file is touched.

Scoped narrowly on purpose — only the module that needs it, only when that
module has already built its temp home.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# Assignees upstream test modules expect to be spawnable, keyed by module name.
_UPSTREAM_EXPECTED_PROFILES = {
    "test_kanban_model_override": ("worker",),
}

# Captured at import time, before any fixture monkeypatches ``Path.home``.
# The modules below patch ``Path.home`` to their tmp_path, so ``Path.home()``
# inside the fixture would compare the temp home against itself and prove
# nothing — this is the only value that still names the operator's real board.
_OPERATOR_HERMES_HOME = Path(os.environ.get("HOME", "/nonexistent")) / ".hermes"


@pytest.fixture(autouse=True)
def _provision_upstream_expected_profiles(request):
    """Create the profile dirs an upstream test module assumes already exist."""
    module = request.module.__name__.rsplit(".", 1)[-1]
    wanted = _UPSTREAM_EXPECTED_PROFILES.get(module)
    if not wanted:
        return
    # The module owns a ``kanban_home`` fixture that sets HERMES_HOME and
    # patches Path.home. Resolve it first so the profiles root points into the
    # temp home rather than the operator's real ~/.hermes.
    if "kanban_home" not in request.fixturenames:
        return
    home = Path(request.getfixturevalue("kanban_home")).resolve()
    assert not home.is_relative_to(_OPERATOR_HERMES_HOME.resolve()), (
        f"refusing to provision profiles inside the operator's real Hermes home: {home}"
    )
    for name in wanted:
        (home / "profiles" / name).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Module-scoped HERMES_HOME template for test_kanban_dashboard_plugin.py
# ---------------------------------------------------------------------------

# The 14 profile names the dashboard-plugin test module provisions per test.
_DASHBOARD_PLUGIN_PROFILES = (
    "default",
    "coder",
    "premium",
    "research",
    "ops",
    "x",
    "a",
    "b",
    "old",
    "new",
    "worker",
    "linguist",
    "alice",
    "bob",
)


@pytest.fixture(scope="module")
def _kanban_plugin_home_template(tmp_path_factory):
    """Module-scoped template: initialized kanban DB + 14 profile dirs.

    Created once per test module; each test receives a ``shutil.copytree``
    of this directory (~250 KiB, <5 ms) instead of re-running ``init_db()``
    (~0.3 s) and 14× ``mkdir`` + ``write_text`` (~0.1 s) per test.

    Same mechanism as ``tests/hermes_cli/conftest.py::_kanban_db_template``
    (commit 64bd92cc4), extended with the profile directories that
    ``test_kanban_dashboard_plugin.py`` needs.
    """
    from hermes_cli import kanban_db as kb

    template_dir = tmp_path_factory.mktemp("kanban_plugin_home")
    home = template_dir / ".hermes"
    home.mkdir()

    # Profile directories with minimal config.yaml.
    profiles = home / "profiles"
    for name in _DASHBOARD_PLUGIN_PROFILES:
        d = profiles / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text("model: {}\n")

    # Initialize the kanban DB at the template path (no HERMES_HOME needed).
    kb.init_db(db_path=home / "kanban.db")

    # Reset the per-process WAL warning dedup so tests that assert on the
    # NFS/SMB fallback warning still observe it for their own connection.
    try:
        from hermes_state import (
            _wal_fallback_warned_paths,
            _wal_reset_bug_warned_paths,
        )

        _wal_fallback_warned_paths.discard("kanban.db (kanban.db)")
        _wal_reset_bug_warned_paths.discard("kanban.db (kanban.db)")
    except ImportError:  # pragma: no cover
        pass

    return home
