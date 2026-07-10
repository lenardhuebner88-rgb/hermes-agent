"""Regression coverage for the ContextVar-scoped board pinning (aa256cd91).

The gateway's ``_auto_decompose_tick`` used to pin the active board by
mutating the PROCESS-GLOBAL ``HERMES_KANBAN_BOARD`` env var for the
duration of an aux-LLM decompose call. Any concurrent bare
``_kb.connect()`` — e.g. the alerts watcher tick — transiently resolved
to that board and evaluated its persistent alert cursors against the
wrong board's row-id space. The fix routes the pin through
``scoped_current_board`` (a ``ContextVar``), which ``get_current_board``
consults BEFORE the env var and which is isolated per
``asyncio.to_thread`` call (contexts are copied per submission).

These pin the two properties the fix relies on:
  * the ContextVar override wins over ``HERMES_KANBAN_BOARD``;
  * a fresh thread (mimicking a concurrent watcher tick that did NOT
    inherit the scope) does NOT see the caller's scoped board.
"""

from __future__ import annotations

import os
import threading

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def two_boards(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(type(tmp_path), "home", lambda: tmp_path, raising=False)
    # Two real boards so board_exists() passes for both slugs.
    for slug in ("default", "alt-board"):
        db_path = kb.kanban_db_path(board=slug)
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        kb.init_db(board=slug)
    return home


def test_scoped_board_wins_over_env(two_boards, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    assert kb.get_current_board() == "default"
    with kb.scoped_current_board("alt-board"):
        assert kb.get_current_board() == "alt-board"
    # Restored after the scope exits.
    assert kb.get_current_board() == "default"


def test_scope_does_not_leak_to_a_fresh_thread(two_boards, monkeypatch):
    """A thread started WITHOUT copying the scope context (the pre-fix env
    mutation's failure mode) must not observe the caller's scoped board."""
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    seen: dict[str, str] = {}
    ready = threading.Event()

    def _worker():
        # threading.Thread does NOT copy the parent's contextvars, so this
        # models a concurrent watcher tick that only sees the env var.
        seen["board"] = kb.get_current_board()
        ready.set()

    with kb.scoped_current_board("alt-board"):
        assert kb.get_current_board() == "alt-board"
        t = threading.Thread(target=_worker)
        t.start()
        ready.wait(timeout=2.0)
        t.join(timeout=2.0)

    # The concurrent thread saw the env default, NOT the caller's scope.
    assert seen["board"] == "default", (
        "board scope leaked across threads — pre-fix env-mutation behavior"
    )
