"""Fork-side test provisioning for upstream plugin tests.

Exists for exactly one job right now: let upstream's own
``test_kanban_model_override.py`` run against the fork instead of dying in
its ``_create`` helper.

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
