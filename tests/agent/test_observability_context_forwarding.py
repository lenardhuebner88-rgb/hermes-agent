"""Regression coverage for worker-only Langfuse correlation forwarding."""

import sqlite3


def test_non_worker_context_is_empty(monkeypatch):
    from hermes_cli.observability_context import resolve_observability_context

    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(name, raising=False)

    assert resolve_observability_context() == {}


def test_worker_context_resolves_and_normalizes_dimensions(monkeypatch):
    import hermes_cli.observability_context as mod

    monkeypatch.setenv("HERMES_KANBAN_TASK", " t-child ")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", " 77 ")
    monkeypatch.setenv("HERMES_PROFILE", " coder ")
    monkeypatch.setattr(
        mod, "_resolve_chain_and_lane", lambda task_id: (" t-root ", " coder ")
    )

    assert mod.resolve_observability_context() == {
        "kanban_task_id": "t-child",
        "task_run_id": "77",
        "chain_id": "t-root",
        "lane": "coder",
        "profile": "coder",
        "origin": "hermes_agent",
    }


def test_missing_or_pseudo_task_is_empty_and_does_not_create_a_board(
    monkeypatch, tmp_path
):
    import hermes_cli.observability_context as mod

    missing_db = tmp_path / "missing" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_TASK", "loop-pack-phase")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(missing_db))

    assert mod.resolve_observability_context() == {}
    assert not missing_db.exists()


def test_exact_worker_identity_survives_unavailable_board_enrichment(
    monkeypatch,
):
    import hermes_cli.observability_context as mod

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-exact")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "run-exact")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "research")
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    monkeypatch.setattr(mod, "_resolve_chain_and_lane", lambda _task_id: (None, None))

    assert mod.resolve_observability_context() == {
        "kanban_task_id": "task-exact",
        "task_run_id": "run-exact",
        "lane": "coder",
        "profile": "coder",
        "board": "research",
        "origin": "hermes_agent",
    }


def test_verified_worker_lookup_is_read_only_and_cached(monkeypatch, tmp_path):
    import hermes_cli.observability_context as mod

    board = tmp_path / "kanban.db"
    real_connect = sqlite3.connect
    with real_connect(board) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT)")
        connection.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT)")
        connection.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
        connection.execute("INSERT INTO tasks VALUES ('task-1', 'coder')")

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    mod._read_worker_dimensions.cache_clear()
    opened = []

    def counted_connect(*args, **kwargs):
        opened.append(args[0])
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(mod.sqlite3, "connect", counted_connect)

    first = mod.resolve_observability_context()
    second = mod.resolve_observability_context()

    assert (
        first
        == second
        == {
            "kanban_task_id": "task-1",
            "chain_id": "task-1",
            "lane": "coder",
            "profile": "coder",
            "origin": "hermes_agent",
        }
    )
    assert len(opened) == 1


def test_negative_worker_lookup_expires_within_the_same_process(
    monkeypatch,
    tmp_path,
):
    import hermes_cli.observability_context as mod

    board = tmp_path / "kanban.db"
    with sqlite3.connect(board) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT)")
        connection.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT)")
        connection.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")

    mod._read_worker_dimensions.cache_clear()

    assert mod._read_worker_dimensions(str(board), "late-task", 2) == (
        None,
        None,
    )
    with sqlite3.connect(board) as connection:
        connection.execute("INSERT INTO tasks VALUES ('late-task', 'coder')")
    # Same five-second bucket remains cached.
    assert mod._read_worker_dimensions(str(board), "late-task", 2) == (
        None,
        None,
    )
    assert mod._read_worker_dimensions(
        str(board),
        "late-task",
        3,
    ) == ("late-task", "coder")
