"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def _kanban_db_template(tmp_path_factory):
    """Module-scoped template: one fully initialized kanban DB.

    ``kb.init_db()`` runs schema creation + all additive migrations and
    stamps ``PRAGMA user_version`` — measured at ~1–2 s on a cold cache.
    Running it once per module and copying the result per test replaces
    that cost with a ~245 KiB file copy (<5 ms).

    The template is created with an explicit ``db_path`` so it does NOT
    depend on ``HERMES_HOME`` being set to any particular value.
    """
    from hermes_cli import kanban_db as kb

    template_dir = tmp_path_factory.mktemp("kanban_db_template")
    template_db = template_dir / "kanban.db"
    kb.init_db(db_path=template_db)

    # Reset the per-process WAL warning dedup so tests that assert on the
    # fallback warning (e.g. test_connect_falls_back_to_delete_on_locking_protocol)
    # still observe it for their own fresh DB connection.  The template init
    # consumed the "kanban.db (kanban.db)" label; clear it.
    try:
        from hermes_state import (
            _wal_fallback_warned_paths,
            _wal_reset_bug_warned_paths,
        )

        _wal_fallback_warned_paths.discard("kanban.db (kanban.db)")
        _wal_reset_bug_warned_paths.discard("kanban.db (kanban.db)")
    except ImportError:  # pragma: no cover
        pass

    return template_dir


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, _kanban_db_template):
    """Isolated HERMES_HOME with a kanban DB copied from the module template.

    Moved from test_kanban_db.py for shared use across split DB test modules.
    Modules that need a different setup (crash grace, kanban env clearing)
    continue to define their own module-level ``kanban_home`` override.

    Instead of calling ``kb.init_db()`` from scratch (~1–2 s per test),
    this copies the pre-initialized template DB (~5 ms).  The copy carries
    the correct ``PRAGMA user_version`` stamp, so ``kb.connect()`` inside
    the test takes the fast path (no schema re-creation, no migration pass).
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    # Fast-copy the pre-initialized template (kanban.db + init.lock).
    for src in _kanban_db_template.iterdir():
        if src.is_file():
            shutil.copy2(src, home / src.name)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


@pytest.fixture(autouse=True)
def _disable_spec_judge_by_default(request, monkeypatch):
    """Disable the PlanSpec quality judge for every test by default.

    ``planspecs.ingest_planspec`` now runs a synchronous LLM judge after the
    deterministic rubric. The auxiliary client auto-detects providers from the
    process environment, so on a box with provider keys exported (e.g. the
    nightly green-gate full-suite run) an unmarked ingest test could fire a
    real, paid model call. Default the judge OFF; tests that exercise it opt in
    with ``@pytest.mark.spec_judge`` and mock the aux client themselves.
    """
    if request.node.get_closest_marker("spec_judge"):
        return
    monkeypatch.setenv("HERMES_PLANSPEC_JUDGE", "0")
