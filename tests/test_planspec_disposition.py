from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db, planspecs


def test_mark_planspec_not_needed_archives_ingested_chain(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    original_connect = kanban_db.connect
    monkeypatch.setattr(kanban_db, "connect", lambda *args, **kwargs: original_connect(db_path=db_path))
    plans_root = tmp_path / "vault" / "03-Agents"
    spec_path = plans_root / "Hermes" / "plans" / "discard-me.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        "---\n"
        "title: Discard me\n"
        "status: open\n"
        "---\n\n"
        "# Discard me\n",
        encoding="utf-8",
    )

    conn = kanban_db.connect()
    try:
        root_id = kanban_db.create_task(
            conn,
            title="PlanSpec discard-me",
            body="HOLD: reviewed/approved PlanSpec is in board but not started",
            assignee="coder",
            created_by="planspec-ingest",
            initial_status="running",
        )
        child_id = kanban_db.create_task(
            conn,
            title="Implement discard-me",
            assignee="coder",
            created_by="planspec-ingest",
            initial_status="running",
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("scheduled", root_id))
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("scheduled", child_id))
        kanban_db.link_tasks(conn, parent_id=child_id, child_id=root_id)
        kanban_db.add_event(
            conn,
            root_id,
            "specified",
            {"source": "planspec_ingest", "path": str(spec_path.resolve(strict=False)), "slice": ""},
        )
        conn.commit()
    finally:
        conn.close()

    result = planspecs.mark_planspec_not_needed(spec_path, plans_root=plans_root, author="test")

    assert result["status"] == "obsolete"
    assert result["archived_chain"]["root_task_id"] == root_id
    conn = kanban_db.connect()
    try:
        assert kanban_db.get_task(conn, root_id).status == "archived"
        assert kanban_db.get_task(conn, child_id).status == "archived"
    finally:
        conn.close()
    text = spec_path.read_text(encoding="utf-8")
    assert "status: obsolete" in text
    assert f"kanban_root_task_id: {root_id}" in text


def test_mark_planspec_not_needed_blocks_running_chain(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    original_connect = kanban_db.connect
    monkeypatch.setattr(kanban_db, "connect", lambda *args, **kwargs: original_connect(db_path=db_path))
    plans_root = tmp_path / "vault" / "03-Agents"
    spec_path = plans_root / "Hermes" / "plans" / "running.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("---\ntitle: Running\nstatus: open\n---\n\n# Running\n", encoding="utf-8")

    conn = kanban_db.connect()
    try:
        root_id = kanban_db.create_task(
            conn, title="PlanSpec running", assignee="coder", created_by="planspec-ingest", initial_status="running"
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("scheduled", root_id))
        child_id = kanban_db.create_task(
            conn, title="Implement running", assignee="coder", created_by="planspec-ingest", initial_status="running"
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("scheduled", root_id))
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("running", child_id))
        kanban_db.link_tasks(conn, parent_id=child_id, child_id=root_id)
        kanban_db.add_event(
            conn,
            root_id,
            "specified",
            {"source": "planspec_ingest", "path": str(spec_path.resolve(strict=False)), "slice": ""},
        )
        conn.commit()
        assert kanban_db.planspec_chain_running_subtasks(conn, root_id) == [child_id]
    finally:
        conn.close()

    try:
        planspecs.mark_planspec_not_needed(spec_path, plans_root=plans_root, author="test")
    except planspecs.PlanSpecBlocked as exc:
        assert "running child task" in "\n".join(exc.findings)
    else:
        raise AssertionError("expected PlanSpecBlocked")

    conn = kanban_db.connect()
    try:
        assert kanban_db.get_task(conn, root_id).status == "scheduled"
        assert kanban_db.get_task(conn, child_id).status == "running"
    finally:
        conn.close()
    assert "status: open" in spec_path.read_text(encoding="utf-8")
