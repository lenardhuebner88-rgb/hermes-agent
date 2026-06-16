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
    closed_path = _write_planspec(plans_root, "2026-06-16-done.md")
    text = closed_path.read_text(encoding="utf-8")
    closed_path.write_text(text.replace("status: freigegeben-komplett", "status: implemented"), encoding="utf-8")
    invalid = plans_root / "Hermes" / "plans" / "draft.md"
    invalid.write_text("# not binding\n", encoding="utf-8")

    records = planspecs.list_planspecs(plans_root=plans_root)
    all_records = planspecs.list_planspecs(plans_root=plans_root, scope="all")

    assert [item["path"] for item in records] == [str(open_path.resolve(strict=False))]
    by_name = {item["filename"]: item for item in all_records}
    assert by_name["2026-06-16-open.md"]["open"] is True
    assert by_name["2026-06-16-done.md"]["open"] is False
    assert by_name["2026-06-16-done.md"]["closed_reason"] == "closed status: implemented"
    assert by_name["draft.md"]["open"] is False
    assert by_name["draft.md"]["closed_reason"] == "invalid PlanSpec"


def test_parse_binding_planspec_blocks_closed_status(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("status: freigegeben-komplett", "status: implemented"), encoding="utf-8")

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.parse_binding_planspec(path, plans_root=plans_root)

    assert "closed status: implemented" in str(exc.value)


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
