from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_planspec(root: Path, name: str = "2026-06-16-B1.md") -> Path:
    path = root / "Hermes" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
status: freigegeben-komplett
owner: Hermes
slice: B1
topic: "Planspec Hub"
freigabe: complete
live_test_depth: contract
taskgraph_hints:
  binding: true
  subtasks:
    - id: B1-S1
      title: "Document schema"
      lane: coder
      deps: []
    - id: B1-S2
      title: "Ingest deterministically"
      lane: coder-claude
      deps: [B1-S1]
---
# B1
""",
        encoding="utf-8",
    )
    return path


def _write_display_plangate(root: Path, name: str = "2026-06-16-abo-limits.md") -> Path:
    path = root / "Claude-Code" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
title: PlanSpec — Dashboard-Tile "Abo-Limits"
status: signiert 2026-06-16 (bereit für Build)
gate: planGate
---
# PlanSpec — Dashboard-Tile "Abo-Limits"
""",
        encoding="utf-8",
    )
    return path


def test_parse_binding_planspec_to_children(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)

    assert spec.topic == "Planspec Hub"
    assert spec.freigabe == "complete"
    assert spec.live_test_depth == "contract"
    assert [child["title"] for child in spec.children] == [
        "Document schema",
        "Ingest deterministically",
    ]
    assert spec.children[1]["parents"] == [0]
    assert spec.children[1]["assignee"] == "coder-claude"


def test_list_planspecs_reports_binding_status(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    records = planspecs.list_planspecs(plans_root=plans_root)

    assert len(records) == 1
    assert records[0]["path"] == str(path.resolve(strict=False))
    assert records[0]["valid"] is True
    assert records[0]["open"] is True
    assert records[0]["closed_reason"] is None
    assert records[0]["subtask_count"] == 2


def test_list_planspecs_defaults_to_open_and_allows_all_scope(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    open_path = _write_planspec(plans_root, "2026-06-16-open.md")
    display_path = _write_display_plangate(plans_root)
    closed_path = _write_planspec(plans_root, "2026-06-16-done.md")
    text = closed_path.read_text(encoding="utf-8")
    closed_path.write_text(text.replace("status: freigegeben-komplett", "status: implemented"), encoding="utf-8")
    invalid = plans_root / "Hermes" / "plans" / "draft.md"
    invalid.write_text("# not binding\n", encoding="utf-8")

    records = planspecs.list_planspecs(plans_root=plans_root)
    all_records = planspecs.list_planspecs(plans_root=plans_root, scope="all")

    assert [item["path"] for item in records] == [
        str(open_path.resolve(strict=False)),
        str(display_path.resolve(strict=False)),
    ]
    by_name = {item["filename"]: item for item in all_records}
    assert by_name["2026-06-16-open.md"]["open"] is True
    assert by_name["2026-06-16-abo-limits.md"]["open"] is True
    assert by_name["2026-06-16-abo-limits.md"]["valid"] is False
    assert by_name["2026-06-16-abo-limits.md"]["binding"] is False
    assert by_name["2026-06-16-abo-limits.md"]["closed_reason"] is None
    assert by_name["2026-06-16-abo-limits.md"]["errors"] == [
        "display-only: taskgraph_hints.binding is missing; Kanban ingest disabled"
    ]
    assert by_name["2026-06-16-done.md"]["open"] is False
    assert by_name["2026-06-16-done.md"]["closed_reason"] == "closed status: implemented"
    assert by_name["draft.md"]["open"] is False
    assert by_name["draft.md"]["closed_reason"] == "invalid PlanSpec"


def test_list_planspecs_filters_valid_and_limits_server_side(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    first_valid = _write_planspec(plans_root, "2026-06-16-alpha.md")
    second_valid = _write_planspec(plans_root, "2026-06-16-beta.md")
    display_path = _write_display_plangate(plans_root)

    records = planspecs.list_planspecs(plans_root=plans_root, valid=True, limit=1)
    invalid_records = planspecs.list_planspecs(plans_root=plans_root, valid=False)
    no_limit = planspecs.list_planspecs(plans_root=plans_root, valid=True, limit=0)

    assert [item["path"] for item in records] == [str(first_valid.resolve(strict=False))]
    assert [item["path"] for item in invalid_records] == [str(display_path.resolve(strict=False))]
    assert [item["path"] for item in no_limit] == [
        str(first_valid.resolve(strict=False)),
        str(second_valid.resolve(strict=False)),
    ]


def test_list_planspecs_searches_topic_filename_agent_and_path(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    alpha = _write_planspec(plans_root, "2026-06-16-alpha.md")
    beta = _write_planspec(plans_root, "2026-06-16-beta.md")
    text = beta.read_text(encoding="utf-8")
    beta.write_text(text.replace('topic: "Planspec Hub"', 'topic: "Release Train"'), encoding="utf-8")

    by_topic = planspecs.list_planspecs(plans_root=plans_root, search="train")
    by_filename = planspecs.list_planspecs(plans_root=plans_root, search="alpha")
    by_agent = planspecs.list_planspecs(plans_root=plans_root, search="Hermes")

    assert [item["path"] for item in by_topic] == [str(beta.resolve(strict=False))]
    assert [item["path"] for item in by_filename] == [str(alpha.resolve(strict=False))]
    assert [item["path"] for item in by_agent] == [
        str(alpha.resolve(strict=False)),
        str(beta.resolve(strict=False)),
    ]


def test_parse_binding_planspec_blocks_closed_status(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("status: freigegeben-komplett", "status: implemented"), encoding="utf-8")

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.parse_binding_planspec(path, plans_root=plans_root)

    assert "closed status: implemented" in str(exc.value)


def test_mark_planspec_not_needed_closes_display_only_plan(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_display_plangate(plans_root)

    result = planspecs.mark_planspec_not_needed(path, plans_root=plans_root, author="tester")
    records = planspecs.list_planspecs(plans_root=plans_root)
    all_records = planspecs.list_planspecs(plans_root=plans_root, scope="all")
    updated = path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["status"] == "obsolete"
    assert records == []
    row = all_records[0]
    assert row["open"] is False
    assert row["closed_reason"] == "closed status: obsolete"
    assert "status: obsolete" in updated
    assert "closed_by: tester" in updated
    assert "closed_reason: not needed anymore" in updated


def test_ingest_planspec_creates_scheduled_children(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert result["ok"] is True
    assert len(result["child_ids"]) == 2
    with kb.connect_closing() as conn:
        root = kb.get_task(conn, result["root_task_id"])
        child1 = kb.get_task(conn, result["child_ids"][0])
        child2 = kb.get_task(conn, result["child_ids"][1])
        assert root is not None
        assert root.status == "todo"
        assert root.tenant == "planspec"
        assert child1 is not None and child1.status == "scheduled"
        assert child1.title == "Document schema"
        assert child1.assignee == "coder"
        assert child2 is not None and child2.status == "scheduled"
        assert child2.assignee == "coder-claude"
        assert kb.parent_ids(conn, child2.id) == [child1.id]
        assert set(kb.parent_ids(conn, root.id)) == {child1.id, child2.id}


def test_list_planspecs_derives_queued_kanban_state(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    records = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)

    assert len(records) == 1
    row = records[0]
    assert row["open"] is True
    assert row["kanban_state"] == "queued"
    assert row["kanban_root_task_id"] == result["root_task_id"]
    assert row["kanban_root_status"] == "todo"
    assert row["kanban_child_total"] == 2
    assert row["kanban_child_done"] == 0


def test_list_planspecs_hides_completed_kanban_state_from_open_scope(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    with kb.connect_closing() as conn:
        child_ids = result["child_ids"]
        with kb.write_txn(conn):
            conn.executemany("UPDATE tasks SET status = 'done' WHERE id = ?", [(task_id,) for task_id in child_ids])
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (result["root_task_id"],))

    assert planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True) == []
    all_records = planspecs.list_planspecs(plans_root=plans_root, scope="all", include_kanban_status=True)

    assert len(all_records) == 1
    row = all_records[0]
    assert row["open"] is False
    assert row["closed_reason"] == "kanban state: completed"
    assert row["kanban_state"] == "completed"
    assert row["kanban_child_done"] == 2


def test_list_planspecs_derives_blocked_and_running_kanban_state(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (result["child_ids"][0],))
    running = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)[0]
    assert running["kanban_state"] == "running"
    assert running["kanban_child_running"] == 1

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (result["child_ids"][1],))
    blocked = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)[0]
    assert blocked["kanban_state"] == "blocked"
    assert blocked["kanban_child_blocked"] == 1


def test_ingest_planspec_is_idempotent_on_reingest(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    second = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert first["ok"] is True
    assert first["already_ingested"] is False
    assert first["idempotency_key"]
    # Re-ingest of the identical file is a no-op that links back to the
    # existing chain instead of minting a duplicate.
    assert second["ok"] is True
    assert second["already_ingested"] is True
    assert second["root_task_id"] == first["root_task_id"]
    assert set(second["child_ids"]) == set(first["child_ids"])
    assert second["subtask_count"] == 2
    assert second["idempotency_key"] == first["idempotency_key"]
    with kb.connect_closing() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        root = kb.get_task(conn, first["root_task_id"])
    # root + 2 subtasks only — the second ingest created nothing.
    assert total == 3
    assert root is not None and root.idempotency_key == first["idempotency_key"]


def test_ingest_planspec_reingest_after_edit_creates_new_chain(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Document schema", "Document schema v2"), encoding="utf-8")
    second = planspecs.ingest_planspec(path, plans_root=plans_root)

    # Edited content -> new content-hash -> new key -> a fresh chain.
    assert second["already_ingested"] is False
    assert second["root_task_id"] != first["root_task_id"]
    assert second["idempotency_key"] != first["idempotency_key"]
    with kb.connect_closing() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    # Two independent chains: 2 * (root + 2 subtasks).
    assert total == 6


def test_sprint_prompt_preserves_binding_subtasks(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    result = planspecs.sprint_prompt_for_planspec(path, plans_root=plans_root)

    assert str(path) in result["prompt"]
    assert "live_test_depth: contract" in result["prompt"]
    assert "B1-S2 · lane=coder-claude deps=[B1-S1]" in result["prompt"]


def test_parse_binding_planspec_blocks_outside_root(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("---\n---\n", encoding="utf-8")

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.parse_binding_planspec(outside, plans_root=tmp_path / "03-Agents")

    assert "must be under" in str(exc.value)
