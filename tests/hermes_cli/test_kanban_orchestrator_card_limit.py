from __future__ import annotations

import json
import logging

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def orchestrator_limit(monkeypatch):
    monkeypatch.setattr(
        kb,
        "_orchestrator_card_limit_config",
        lambda: (3, "default"),
    )


def _create_root(conn, title: str) -> str:
    return kb.create_task(conn, title=title, created_by="seed")


def _create_orchestrator_child(conn, root_id: str, number: int) -> str:
    return kb.create_task(
        conn,
        title=f"orchestrated-{number}",
        parents=[root_id],
        created_by="default",
    )


def test_fourth_orchestrator_card_blocks_latest_nonterminal_without_insert(
    kanban_home, monkeypatch, orchestrator_limit
):
    clock = iter(range(100, 200))
    monkeypatch.setattr(kb.time, "time", lambda: next(clock))

    with kb.connect_closing() as conn:
        root_id = _create_root(conn, "limited-root")
        created = [_create_orchestrator_child(conn, root_id, index) for index in range(1, 4)]
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (created[-1],)
        ).fetchone()["status"] == "todo"

        with pytest.raises(kb.OrchestratorCardLimitReached) as raised:
            _create_orchestrator_child(conn, root_id, 4)

        blocked_id = raised.value.blocked_id
        assert blocked_id == created[-1]
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE 'orchestrated-%'"
        ).fetchone()[0] == 3
        blocked = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (blocked_id,)
        ).fetchone()
        assert dict(blocked) == {"status": "blocked", "block_kind": "capability"}
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (blocked_id,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["root_id"] == root_id
        assert payload["count"] == 3
        assert payload["attempted_count"] == 4
        assert root_id in payload["reason"]
        assert "count=3" in payload["reason"]

        control_root = _create_root(conn, "control-root")
        control_cards = [
            _create_orchestrator_child(conn, control_root, index) for index in range(10, 12)
        ]
        statuses = conn.execute(
            "SELECT status FROM tasks WHERE id IN (?, ?) ORDER BY id",
            tuple(control_cards),
        ).fetchall()
        assert {row["status"] for row in statuses} == {"todo"}


def test_decomposed_chain_inverse_links_count_once_and_block_fourth(
    kanban_home, monkeypatch, orchestrator_limit
):
    clock = iter(range(150, 250))
    monkeypatch.setattr(kb.time, "time", lambda: next(clock))

    with kb.connect_closing() as conn:
        root_id = _create_root(conn, "decomposed-root")
        created = [
            kb.create_task(
                conn,
                title=f"decomposed-{index}",
                created_by="default",
            )
            for index in range(1, 4)
        ]
        for child_id in created:
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (child_id, root_id),
            )
        # Exercise UNION de-duplication when a card is reachable in both directions.
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (root_id, created[0]),
        )
        kb._append_event(
            conn,
            root_id,
            "decomposed",
            {"child_ids": created},
        )

        from hermes_cli import kanban_worktrees as kwt

        assert [kwt.chain_root_id(conn, child_id) for child_id in created] == [
            root_id,
            root_id,
            root_id,
        ]
        with pytest.raises(kb.OrchestratorCardLimitReached) as raised:
            kb.create_task(
                conn,
                title="decomposed-4",
                parents=[created[0]],
                created_by="default",
            )

        assert raised.value.root_id == root_id
        assert raised.value.blocked_id == created[-1]
        assert raised.value.count == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title GLOB 'decomposed-[0-9]'"
        ).fetchone()[0] == 3


def test_orchestrator_created_root_counts_toward_limit(
    kanban_home, monkeypatch, orchestrator_limit
):
    clock = iter(range(200, 300))
    monkeypatch.setattr(kb.time, "time", lambda: next(clock))

    with kb.connect_closing() as conn:
        root_id = kb.create_task(
            conn,
            title="orchestrator-created-root",
            created_by="default",
        )
        created = [
            _create_orchestrator_child(conn, root_id, index) for index in range(1, 3)
        ]

        with pytest.raises(kb.OrchestratorCardLimitReached) as raised:
            _create_orchestrator_child(conn, root_id, 3)

        assert raised.value.root_id == root_id
        assert raised.value.blocked_id == created[-1]
        assert raised.value.count == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_by = 'default'"
        ).fetchone()[0] == 3


def test_only_exact_configured_creator_counts(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb,
        "_orchestrator_card_limit_config",
        lambda: (3, "dispatch-orchestrator"),
    )
    clock = iter(range(300, 400))
    monkeypatch.setattr(kb.time, "time", lambda: next(clock))

    with kb.connect_closing() as conn:
        root_id = _create_root(conn, "creator-root")
        ignored_ids = [
            kb.create_task(
                conn,
                title=f"fixer-{index}",
                parents=[root_id],
                created_by="orchestrator",
            )
            for index in range(4)
        ]
        ignored_ids.extend(
            kb.create_task(
                conn,
                title=f"review-{index}",
                parents=[root_id],
                created_by="kanban-review-chain",
            )
            for index in range(4)
        )
        ignored_ids.append(
            kb.create_task(
                conn,
                title="default-profile",
                parents=[root_id],
                created_by="default",
            )
        )

        counted = [
            kb.create_task(
                conn,
                title=f"dispatch-{index}",
                parents=[root_id],
                created_by="dispatch-orchestrator",
            )
            for index in range(1, 4)
        ]
        with pytest.raises(kb.OrchestratorCardLimitReached) as raised:
            kb.create_task(
                conn,
                title="dispatch-4",
                parents=[root_id],
                created_by="dispatch-orchestrator",
            )

        assert raised.value.blocked_id == counted[-1]
        ignored = conn.execute(
            "SELECT status FROM tasks WHERE id IN (" + ",".join("?" * len(ignored_ids)) + ")",
            tuple(ignored_ids),
        ).fetchall()
        assert all(row["status"] == "todo" for row in ignored)


def test_limit_blocks_latest_qualifying_card_not_newer_foreign_card(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(
        kb,
        "_orchestrator_card_limit_config",
        lambda: (3, "dispatch-orchestrator"),
    )
    clock = iter(range(400, 500))
    monkeypatch.setattr(kb.time, "time", lambda: next(clock))

    with kb.connect_closing() as conn:
        root_id = _create_root(conn, "mixed-creator-root")
        counted = [
            kb.create_task(
                conn,
                title=f"dispatch-{index}",
                parents=[root_id],
                created_by="dispatch-orchestrator",
            )
            for index in range(1, 4)
        ]
        foreign_id = kb.create_task(
            conn,
            title="newer-coder-card",
            parents=[root_id],
            created_by="coder",
        )

        with pytest.raises(kb.OrchestratorCardLimitReached) as raised:
            kb.create_task(
                conn,
                title="dispatch-4",
                parents=[root_id],
                created_by="dispatch-orchestrator",
            )

        assert raised.value.blocked_id == counted[-1]
        assert raised.value.count == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title = 'dispatch-4'"
        ).fetchone()[0] == 0
        statuses = conn.execute(
            "SELECT id, status FROM tasks WHERE id IN (?, ?)",
            (counted[-1], foreign_id),
        ).fetchall()
        assert {row["id"]: row["status"] for row in statuses} == {
            counted[-1]: "blocked",
            foreign_id: "todo",
        }


def test_limit_without_live_qualifying_card_rejects_without_blocking_foreign_tasks(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(
        kb,
        "_orchestrator_card_limit_config",
        lambda: (3, "dispatch-orchestrator"),
    )

    with kb.connect_closing() as conn:
        root_id = _create_root(conn, "foreign-root")
        counted = [
            kb.create_task(
                conn,
                title=f"terminal-dispatch-{index}",
                parents=[root_id],
                created_by="dispatch-orchestrator",
            )
            for index in range(1, 4)
        ]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id IN (?, ?, ?)",
                tuple(counted),
            )
        foreign_id = kb.create_task(
            conn,
            title="live-foreign-card",
            parents=[root_id],
            created_by="coder",
        )

        with pytest.raises(kb.OrchestratorCardLimitReached) as raised:
            kb.create_task(
                conn,
                title="rejected-dispatch-4",
                parents=[root_id],
                created_by="dispatch-orchestrator",
            )

        assert raised.value.root_id == root_id
        assert raised.value.blocked_id is None
        assert raised.value.count == 3
        assert "no existing task blocked" in str(raised.value)
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title = 'rejected-dispatch-4'"
        ).fetchone()[0] == 0
        statuses = conn.execute(
            "SELECT id, status FROM tasks WHERE id IN (?, ?, ?, ?, ?)",
            (root_id, *counted, foreign_id),
        ).fetchall()
        assert {row["id"]: row["status"] for row in statuses} == {
            root_id: "ready",
            **{task_id: "done" for task_id in counted},
            foreign_id: "todo",
        }
        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'orchestrator_card_limit_rejected' "
            "ORDER BY id DESC LIMIT 1",
            (root_id,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["blocked_id"] is None
        assert payload["count"] == 3
        assert payload["attempted_count"] == 4
        assert payload["orchestrator_profile"] == "dispatch-orchestrator"


def test_profile_named_orchestrator_disables_limit_loudly(
    kanban_home, monkeypatch, caplog
):
    monkeypatch.setattr(
        kb,
        "_orchestrator_card_limit_config",
        lambda: (3, "orchestrator"),
    )
    caplog.set_level(logging.ERROR, logger=kb.__name__)

    with kb.connect_closing() as conn:
        root_id = _create_root(conn, "fixer-safe-root")
        created = [
            kb.create_task(
                conn,
                title=f"fixer-safe-{index}",
                parents=[root_id],
                created_by="orchestrator",
            )
            for index in range(4)
        ]

        assert len(set(created)) == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title GLOB 'fixer-safe-[0-9]'"
        ).fetchone()[0] == 4
    assert "orchestrator_card_limit disabled" in caplog.text
    assert "kanban.orchestrator_profile='orchestrator'" in caplog.text


def test_zero_limit_preserves_unbounded_creation(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb,
        "_orchestrator_card_limit_config",
        lambda: (0, "default"),
    )

    with kb.connect_closing() as conn:
        root_id = _create_root(conn, "disabled-root")
        created = [_create_orchestrator_child(conn, root_id, index) for index in range(4)]

        assert len(set(created)) == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE 'orchestrated-%'"
        ).fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'blocked'"
        ).fetchone()[0] == 0


def test_parentless_cards_are_not_counted_as_one_chain(kanban_home, orchestrator_limit):
    with kb.connect_closing() as conn:
        created = [
            kb.create_task(conn, title=f"root-{index}", created_by="default")
            for index in range(4)
        ]

        assert len(set(created)) == 4
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 4


def test_multiple_parents_use_smallest_parent_chain_root(
    kanban_home, monkeypatch, orchestrator_limit
):
    clock = iter(range(500, 600))
    monkeypatch.setattr(kb.time, "time", lambda: next(clock))

    with kb.connect_closing() as conn:
        roots = sorted([_create_root(conn, "root-a"), _create_root(conn, "root-b")])
        smaller_root, larger_root = roots
        counted = [
            _create_orchestrator_child(conn, smaller_root, index) for index in range(1, 4)
        ]

        with pytest.raises(kb.OrchestratorCardLimitReached) as raised:
            kb.create_task(
                conn,
                title="multi-parent-fourth",
                parents=[larger_root, smaller_root],
                created_by="default",
            )

        assert raised.value.blocked_id == counted[-1]
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title = 'multi-parent-fourth'"
        ).fetchone()[0] == 0


def test_reads_limit_and_profile_from_root_config(kanban_home):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  orchestrator_card_limit: 3\n  orchestrator_profile: dispatch-orchestrator\n",
        encoding="utf-8",
    )

    assert kb._orchestrator_card_limit_config() == (3, "dispatch-orchestrator")
