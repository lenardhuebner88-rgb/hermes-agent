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
acceptance_criteria:
  - "Slice ships its deliverable with the targeted gates green"
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


def test_parse_binding_planspec_maps_reviewer_lane_to_review_kind(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            """    - id: B1-S2
      title: "Ingest deterministically"
      lane: coder-claude
      deps: [B1-S1]
""",
            """    - id: B1-S2
      title: "Final verdict"
      lane: reviewer
      deps: [B1-S1]
""",
        ),
        encoding="utf-8",
    )

    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)

    assert spec.children[0]["kind"] == "code"
    assert spec.children[1]["assignee"] == "reviewer"
    assert spec.children[1]["kind"] == "review"


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


def _active_planspec_roots(conn) -> list:
    # PlanSpec roots are the only tasks carrying a ``planspec-ingest:`` key;
    # their subtasks share ``tenant='planspec'`` but have no idempotency_key.
    return conn.execute(
        "SELECT id, status, idempotency_key FROM tasks "
        "WHERE tenant = 'planspec' AND status != 'archived' "
        "AND idempotency_key LIKE 'planspec-ingest:%' "
        "ORDER BY created_at"
    ).fetchall()


# --- AC-F6: changed spec must not silently duplicate its chain --------------


def test_ac_f6_unchanged_reingest_stays_noop(kanban_home, tmp_path: Path):
    """Case 1/4: a byte-identical re-ingest links back, mints nothing."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    second = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert second["already_ingested"] is True
    assert second["root_task_id"] == first["root_task_id"]
    with kb.connect_closing() as conn:
        roots = _active_planspec_roots(conn)
    assert [r["id"] for r in roots] == [first["root_task_id"]]


def test_ac_f6_changed_spec_without_supersede_aborts_with_diff(kanban_home, tmp_path: Path):
    """Case 2/4: an edited spec aborts (no duplicate) and the block names the diff."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Document schema", "Document schema v2"), encoding="utf-8")

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    findings = "\n".join(exc.value.findings)
    assert "--supersede" in findings
    # the diff must name what changed (the B1-S1 subtask title)
    assert "B1-S1" in findings
    with kb.connect_closing() as conn:
        roots = _active_planspec_roots(conn)
    # the abort created nothing: only the original chain remains
    assert [r["id"] for r in roots] == [first["root_task_id"]]
    assert roots[0]["idempotency_key"] == first["idempotency_key"]


def test_ac_f6_supersede_archives_old_chain_and_creates_new(kanban_home, tmp_path: Path):
    """Case 3/4: --supersede archives the prior chain, then ingests the new one."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Document schema", "Document schema v2"), encoding="utf-8")

    second = planspecs.ingest_planspec(path, plans_root=plans_root, supersede=True)

    assert second["already_ingested"] is False
    assert second["root_task_id"] != first["root_task_id"]
    assert second["idempotency_key"] != first["idempotency_key"]
    assert second.get("superseded") == [first["root_task_id"]]
    with kb.connect_closing() as conn:
        old_root = kb.get_task(conn, first["root_task_id"])
        old_children = [kb.get_task(conn, cid) for cid in first["child_ids"]]
        roots = _active_planspec_roots(conn)
    # old chain fully archived, new chain is the only live one
    assert old_root is not None and old_root.status == "archived"
    assert all(c is not None and c.status == "archived" for c in old_children)
    assert [r["id"] for r in roots] == [second["root_task_id"]]
    # re-ingesting the new content is now a plain no-op
    third = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert third["already_ingested"] is True
    assert third["root_task_id"] == second["root_task_id"]


def test_ac_f6_supersede_refused_when_old_chain_has_running_children(kanban_home, tmp_path: Path):
    """Case 4/4: --supersede refuses while a prior subtask is still running."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?",
                (first["child_ids"][0],),
            )
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Document schema", "Document schema v2"), encoding="utf-8")

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root, supersede=True)

    findings = "\n".join(exc.value.findings).lower()
    assert "running" in findings and "operator" in findings
    with kb.connect_closing() as conn:
        old_root = kb.get_task(conn, first["root_task_id"])
        roots = _active_planspec_roots(conn)
    # refusal left the prior chain untouched and minted nothing
    assert old_root is not None and old_root.status != "archived"
    assert [r["id"] for r in roots] == [first["root_task_id"]]


def test_ac_f6_identity_tracks_slice_across_a_moved_file(kanban_home, tmp_path: Path):
    """Robustness: same frontmatter ``slice`` at a new path is the same identity."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root, "2026-06-19-orig.md")
    first = planspecs.ingest_planspec(path, plans_root=plans_root)

    # Move the spec (new path) but keep slice B1 and change a subtask.
    moved = _write_planspec(plans_root, "2026-06-19-moved.md")
    text = moved.read_text(encoding="utf-8")
    moved.write_text(text.replace("Ingest deterministically", "Ingest deterministically v2"), encoding="utf-8")

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(moved, plans_root=plans_root)
    assert "--supersede" in "\n".join(exc.value.findings)

    second = planspecs.ingest_planspec(moved, plans_root=plans_root, supersede=True)
    assert second.get("superseded") == [first["root_task_id"]]
    with kb.connect_closing() as conn:
        roots = _active_planspec_roots(conn)
    assert [r["id"] for r in roots] == [second["root_task_id"]]


def test_ac_f6_dep_change_detected_in_diff(kanban_home, tmp_path: Path):
    """Gap fix: a re-ingest where only a subtask's deps changed must abort with
    a diff that names the dependency change — NOT the generic 'no structural
    field diff' fallback message."""
    plans_root = tmp_path / "03-Agents"
    path = plans_root / "Hermes" / "plans" / "2026-06-19-dep-change.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    # v1: B1-S2 depends on B1-S1
    path.write_text(
        """---
status: freigegeben-komplett
owner: Hermes
slice: B1-deptest
topic: "Dep change test"
freigabe: complete
live_test_depth: contract
acceptance_criteria:
  - "Gates green"
taskgraph_hints:
  binding: true
  subtasks:
    - id: B1-S1
      title: "Step one"
      lane: coder
      deps: []
    - id: B1-S2
      title: "Step two"
      lane: coder-claude
      deps: [B1-S1]
---
# Dep change test
""",
        encoding="utf-8",
    )
    first = planspecs.ingest_planspec(path, plans_root=plans_root)

    # v2: only B1-S2's deps removed (nothing else changed structurally)
    path.write_text(
        """---
status: freigegeben-komplett
owner: Hermes
slice: B1-deptest
topic: "Dep change test"
freigabe: complete
live_test_depth: contract
acceptance_criteria:
  - "Gates green"
taskgraph_hints:
  binding: true
  subtasks:
    - id: B1-S1
      title: "Step one"
      lane: coder
      deps: []
    - id: B1-S2
      title: "Step two"
      lane: coder-claude
      deps: []
---
# Dep change test
""",
        encoding="utf-8",
    )

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)

    findings = "\n".join(exc.value.findings)
    # must abort (no duplicate chain)
    assert "--supersede" in findings
    # diff must name the subtask and the dep change
    assert "B1-S2" in findings
    assert "B1-S1" in findings
    # must NOT produce the misleading no-structural-diff fallback
    assert "no structural field diff" not in findings

    # board must still have only the original chain
    with kb.connect_closing() as conn:
        roots = _active_planspec_roots(conn)
    assert [r["id"] for r in roots] == [first["root_task_id"]]


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
