from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs
from hermes_cli.subcommands import plan as plan_subcommand


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task_count() -> int:
    with kb.connect_closing() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


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


def _set_planspec_board(path: Path, board: str) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("freigabe: complete\n", f"freigabe: complete\nboard: {board}\n"),
        encoding="utf-8",
    )


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


def _write_prose_plan(root: Path, name: str = "prose-plan.md") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """# Prose Pipeline Plan
**Goal:** Turn prose into deterministic board children.

## Slice: Parse prose
- done-when: Parser returns a structured plan.

## Slice: Compile children
- done-when: Compiler returns child dictionaries.
""",
        encoding="utf-8",
    )
    return path


def test_plan_list_json_requests_existing_kanban_bindings(monkeypatch, capsys):
    expected = [
        {"path": "/active.md", "kanban_state": "running", "kanban_root_task_id": "t_active"},
        {"path": "/closed.md", "kanban_state": "completed", "kanban_root_task_id": "t_closed"},
    ]

    def fake_list_planspecs(*, scope, include_kanban_status=False):
        assert scope == "all"
        assert include_kanban_status is True
        return expected

    monkeypatch.setattr(plan_subcommand.planspecs, "list_planspecs", fake_list_planspecs)

    rc = plan_subcommand.plan_command(Namespace(plan_action="list", all=True, json=True))

    assert rc == 0
    assert capsys.readouterr().out == (
        '{"planspecs": [{"path": "/active.md", "kanban_state": "running", '
        '"kanban_root_task_id": "t_active"}, {"path": "/closed.md", '
        '"kanban_state": "completed", "kanban_root_task_id": "t_closed"}]}\n'
    )


def test_compile_preview_prints_children(kanban_home, tmp_path: Path, capsys):
    path = _write_prose_plan(tmp_path)

    rc = plan_subcommand.plan_command(
        Namespace(plan_action="compile", path=str(path), json=False)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Proposed children" in out
    assert "Parse prose" in out
    assert "Compile children" in out
    assert "repairs:" in out
    assert _task_count() == 0


def test_ingest_accepts_prose(kanban_home, tmp_path: Path, capsys):
    path = _write_prose_plan(tmp_path)

    rc = plan_subcommand.plan_command(
        Namespace(
            plan_action="ingest",
            path=str(path),
            board=None,
            author="prose-test",
            json=False,
            force=False,
            supersede=False,
        )
    )

    assert rc == 0
    assert "Ingested" in capsys.readouterr().out
    assert _task_count() == 3
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT title, assignee, planspec_source FROM tasks ORDER BY created_at, id"
        ).fetchall()
    assert any(row["title"].startswith("PlanSpec prose-plan") for row in rows)
    children = [row for row in rows if row["planspec_source"]]
    assert {row["title"] for row in children} == {"Parse prose", "Compile children"}
    assert {row["planspec_source"] for row in children} == {str(path.resolve(strict=False))}


def test_ingest_binding_unchanged(monkeypatch, tmp_path: Path, capsys):
    path = _write_planspec(tmp_path / "03-Agents")
    captured: dict[str, object] = {}

    def fake_ingest(path_arg, **kwargs):
        captured["path"] = path_arg
        captured["kwargs"] = kwargs
        return {
            "ok": True,
            "already_ingested": False,
            "path": str(path),
            "root_task_id": "t_root",
            "child_ids": ["t_child"],
            "initial_child_status": "todo",
            "superseded": [],
        }

    monkeypatch.setattr(plan_subcommand.planspecs, "ingest_planspec", fake_ingest)

    rc = plan_subcommand.plan_command(
        Namespace(
            plan_action="ingest",
            path=str(path),
            board="board-a",
            author="binding-test",
            json=False,
            force=True,
            supersede=True,
        )
    )

    assert rc == 0
    assert "Ingested" in capsys.readouterr().out
    assert captured["path"] == str(path)
    assert captured["kwargs"] == {
        "board": "board-a",
        "author": "binding-test",
        "force": True,
        "supersede": True,
    }


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


def test_ingest_planspec_frontmatter_board_sets_explicit_code_workspaces(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    _set_planspec_board(path, "health-track")
    repo = tmp_path / "health-track"
    repo.mkdir()
    kb.create_board("health-track", default_workdir=str(repo))

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert all(child["workspace_kind"] == "dir" for child in result["children"])
    assert all(child["workspace_path"] == str(repo) for child in result["children"])
    with kb.connect_closing(board="health-track") as conn:
        rows = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks "
            "WHERE id IN (?, ?) ORDER BY id",
            tuple(result["child_ids"]),
        ).fetchall()
    assert [(row["workspace_kind"], row["workspace_path"]) for row in rows] == [
        ("dir", str(repo)),
        ("dir", str(repo)),
    ]


def test_ingest_planspec_cli_board_overrides_frontmatter_board(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    _set_planspec_board(path, "health-track")
    health_repo = tmp_path / "health-track"
    cli_repo = tmp_path / "cli-repo"
    health_repo.mkdir()
    cli_repo.mkdir()
    kb.create_board("health-track", default_workdir=str(health_repo))
    kb.create_board("cli-board", default_workdir=str(cli_repo))

    result = planspecs.ingest_planspec(
        path, plans_root=plans_root, board="cli-board"
    )

    assert all(child["workspace_path"] == str(cli_repo) for child in result["children"])
    with kb.connect_closing(board="health-track") as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0
    with kb.connect_closing(board="cli-board") as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 3


def test_unknown_frontmatter_board_blocks_validate_and_ingest(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    _set_planspec_board(path, "gibts-nicht")

    preview = planspecs.validate_planspec(path, plans_root=plans_root)

    assert preview["disposition"] == "invalid"
    assert preview["would_block"] is True
    assert preview["findings"] == ["unknown board slug: gibts-nicht"]
    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)
    assert exc.value.findings == ["unknown board slug: gibts-nicht"]
    assert _task_count() == 0


def test_plan_validate_shows_target_board(
    kanban_home, tmp_path: Path, capsys, monkeypatch
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    _set_planspec_board(path, "health-track")
    kb.create_board("health-track", default_workdir=str(tmp_path / "health-track"))
    preview = planspecs.validate_planspec(path, plans_root=plans_root)
    assert preview["board"] == "health-track"
    monkeypatch.setattr(
        plan_subcommand.planspecs,
        "validate_planspec",
        lambda path, *, board=None: preview,
    )

    rc = plan_subcommand.plan_command(
        Namespace(plan_action="validate", path=str(path), board=None, json=False)
    )

    assert rc == 0
    assert "board=health-track" in capsys.readouterr().out


def test_verdict_only_code_lane_warns_on_validate_and_blocks_ingest(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            """      title: "Document schema"
      lane: coder
      deps: []
""",
            """      title: "Recon existing behavior"
      lane: research
      kind: code
      deps: []
""",
        ),
        encoding="utf-8",
    )

    preview = planspecs.validate_planspec(path, plans_root=plans_root)

    assert preview["disposition"] == "warn"
    assert preview["would_block"] is True
    assert len(preview["findings"]) == 1
    finding = preview["findings"][0]
    assert "role_misuse" in finding
    assert "lane='research' cannot own kind='code'" in finding
    assert "coder, coder-claude, premium" in finding
    assert "scout" in finding

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root)
    assert exc.value.findings == [finding]
    assert _task_count() == 0


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


def test_subtask_review_tier_flows_into_child():
    """B-T3: a subtask's review_tier is parsed and threaded into the child dict;
    an unset review_tier leaves the key absent (byte-identical to today)."""
    from hermes_cli.plan_compiler import taskgraph_hints_to_children

    children = taskgraph_hints_to_children(
        {
            "binding": True,
            "subtasks": [
                {"id": "s1", "title": "migrate db", "lane": "coder", "review_tier": "critical"},
                {"id": "s2", "title": "plain change", "lane": "coder"},
            ],
        }
    )
    assert children[0]["review_tier"] == "critical"
    assert "review_tier" not in children[1]


def test_valid_review_tiers_constant_exposed():
    assert {"standard", "review", "critical"} <= planspecs.VALID_REVIEW_TIERS


def test_invalid_review_tier_is_flagged(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            """    - id: B1-S1
      title: "Document schema"
      lane: coder
      deps: []
""",
            """    - id: B1-S1
      title: "Document schema"
      lane: coder
      deps: []
      review_tier: bogus
""",
        ),
        encoding="utf-8",
    )
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    findings = planspecs._collect_spec_rubric_findings(spec)
    assert any("review_tier" in f and "B1-S1" in f for f in findings)


def test_valid_review_tier_passes_rubric(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            """    - id: B1-S1
      title: "Document schema"
      lane: coder
      deps: []
""",
            """    - id: B1-S1
      title: "Document schema"
      lane: coder
      deps: []
      review_tier: critical
""",
        ),
        encoding="utf-8",
    )
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    findings = planspecs._collect_spec_rubric_findings(spec)
    assert not any("review_tier" in f for f in findings)


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


def test_list_planspecs_treats_archived_kanban_root_as_closed(tmp_path: Path, monkeypatch):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root, "2026-06-21-archived.md")

    def _fake_states(paths: list[Path]) -> dict[str, dict[str, dict[str, object]]]:
        return {
            "default": {
                str(item.resolve(strict=False)): {"state": "archived", "root_task_id": "t_archived"}
                for item in paths
            }
        }

    assert path.exists()
    monkeypatch.setattr(planspecs, "_planspec_kanban_states", _fake_states)

    open_records = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)
    all_records = planspecs.list_planspecs(
        plans_root=plans_root, scope="all", include_kanban_status=True
    )

    assert open_records == []
    by_name = {item["filename"]: item for item in all_records}
    row = by_name["2026-06-21-archived.md"]
    assert row["open"] is False
    assert row["closed_reason"] == "kanban state: archived"


@pytest.mark.parametrize("live_state", ["running", "queued", "blocked"])
def test_list_planspecs_keeps_live_kanban_root_open(tmp_path: Path, monkeypatch, live_state):
    plans_root = tmp_path / "03-Agents"
    _write_planspec(plans_root, "2026-06-21-live.md")

    def _fake_state(path: Path, *, board: str | None = None) -> dict[str, object]:
        return {"state": live_state, "root_task_id": "t_live"}

    monkeypatch.setattr(planspecs, "_planspec_kanban_state", _fake_state)

    records = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)

    assert [item["filename"] for item in records] == ["2026-06-21-live.md"]
    assert records[0]["open"] is True
    assert records[0]["closed_reason"] is None


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



def test_list_planspecs_bounds_ingest_precheck_after_filters_and_limit(
    tmp_path: Path, monkeypatch
):
    """The dashboard list is polled frequently; precheck must not scan every
    binding PlanSpec before scope/search/limit are applied."""
    plans_root = tmp_path / "03-Agents"
    alpha = _write_planspec(plans_root, "2026-06-16-alpha.md")
    _write_planspec(plans_root, "2026-06-16-beta.md")
    _write_display_plangate(plans_root)
    seen: list[str] = []

    def fake_validate(path: Path, *, plans_root: Path = planspecs.DEFAULT_PLANS_ROOT):
        seen.append(Path(path).name)
        return {"disposition": "clean", "would_block": False, "findings": []}

    monkeypatch.setattr(planspecs, "validate_planspec", fake_validate)

    records = planspecs.list_planspecs(
        plans_root=plans_root, scope="all", valid=True, search="alpha", limit=1
    )

    assert [item["path"] for item in records] == [str(alpha.resolve(strict=False))]
    assert seen == ["2026-06-16-alpha.md"]
    assert records[0]["ingest_disposition"] == "clean"
    assert records[0]["ingest_would_block"] is False

def test_list_planspecs_includes_ingest_precheck_fields(tmp_path: Path):
    """list_planspecs records carry ingest_disposition, ingest_would_block, and
    ingest_findings so the dashboard can show inline blockers before the
    operator clicks the Kanban button."""
    plans_root = tmp_path / "03-Agents"
    open_path = _write_planspec(plans_root, "2026-06-16-open.md")
    display_path = _write_display_plangate(plans_root)

    records = planspecs.list_planspecs(plans_root=plans_root, scope="all")

    by_name = {item["filename"]: item for item in records}
    # Binding + valid spec: precheck ran, so disposition is not the default
    open_rec = by_name["2026-06-16-open.md"]
    assert open_rec["binding"] is True
    assert open_rec["valid"] is True
    assert "ingest_disposition" in open_rec
    assert "ingest_would_block" in open_rec
    assert "ingest_findings" in open_rec
    assert isinstance(open_rec["ingest_findings"], list)
    # Non-binding display-only spec: never ingestable from list
    display_rec = by_name["2026-06-16-abo-limits.md"]
    assert display_rec["binding"] is False
    assert display_rec["ingest_disposition"] == "not_ingestable"
    assert display_rec["ingest_would_block"] is True
    assert display_rec["ingest_findings"] == []


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


def test_mark_planspec_shipped_requires_terminal_or_receipt_evidence(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.mark_planspec_shipped(path, plans_root=plans_root, author="tester")

    assert "Kanban terminal state or release/receipt evidence is required" in str(exc.value)


def test_mark_planspec_shipped_closes_with_metadata_and_is_idempotent(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    first = planspecs.mark_planspec_shipped(
        path,
        plans_root=plans_root,
        author="tester",
        kanban_state="completed",
        receipt="/home/piet/vault/03-Agents/Hermes/receipts/ship.md",
        kanban_root_task_id="t_root1234",
    )
    second = planspecs.mark_planspec_shipped(
        path,
        plans_root=plans_root,
        author="someone-else",
        kanban_state="completed",
        receipt="/home/piet/vault/03-Agents/Hermes/receipts/other.md",
        kanban_root_task_id="t_other",
    )
    open_records = planspecs.list_planspecs(plans_root=plans_root)
    all_records = planspecs.list_planspecs(plans_root=plans_root, scope="all")
    updated = path.read_text(encoding="utf-8")

    assert first["ok"] is True
    assert first["status"] == "shipped"
    assert first["idempotent"] is False
    assert second["ok"] is True
    assert second["status"] == "shipped"
    assert second["idempotent"] is True
    assert open_records == []
    row = all_records[0]
    assert row["open"] is False
    assert row["closed_reason"] == "closed status: shipped"
    assert row["closed_by"] == "tester"
    assert row["receipt"] == "/home/piet/vault/03-Agents/Hermes/receipts/ship.md"
    assert row["kanban_root_task_id"] == "t_root1234"
    assert "status: shipped" in updated
    assert "closed_by: tester" in updated
    assert "closed_reason: shipped" in updated
    assert "receipt: /home/piet/vault/03-Agents/Hermes/receipts/ship.md" in updated
    assert "kanban_root_task_id: t_root1234" in updated


def test_plan_shipped_cli_json(tmp_path: Path, capsys):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    rc = plan_subcommand.plan_command(
        Namespace(
            plan_action="shipped",
            path=str(path),
            author="cli-tester",
            kanban_state="completed",
            receipt=None,
            release_evidence=None,
            kanban_root_task_id="t_root1234",
            json=True,
            plans_root=plans_root,
        )
    )

    assert rc == 0
    assert '\"status\": \"shipped\"' in capsys.readouterr().out


def test_ingest_planspec_creates_todo_children_for_complete_freigabe(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)

    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    assert result["ok"] is True
    assert len(result["child_ids"]) == 2
    assert result["initial_child_status"] == "todo"
    with kb.connect_closing() as conn:
        root = kb.get_task(conn, result["root_task_id"])
        child1 = kb.get_task(conn, result["child_ids"][0])
        child2 = kb.get_task(conn, result["child_ids"][1])
        assert root is not None
        assert root.status == "todo"
        assert root.tenant == "planspec"
        assert child1 is not None and child1.status == "todo"
        assert child1.title == "Document schema"
        assert child1.assignee == "coder"
        assert child2 is not None and child2.status == "todo"
        # Phase A back-compat: a planspec naming lane `coder-claude` still ingests,
        # and the child routes to the canonical Claude coder lane `premium`.
        assert child2.assignee == "premium"
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


def test_list_planspecs_lifecycle_scope_separates_closed_and_open_records(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    shipped_path = _write_planspec(plans_root, "2026-06-16-shipped.md")
    obsolete_path = _write_planspec(plans_root, "2026-06-16-obsolete.md")
    completed_path = _write_planspec(plans_root, "2026-06-16-completed-kanban.md")
    archived_path = _write_planspec(plans_root, "2026-06-16-archived-kanban.md")
    open_path = _write_planspec(plans_root, "2026-06-16-not-ingested-open.md")
    for path, slice_id in (
        (shipped_path, "LC-SHIPPED"),
        (obsolete_path, "LC-OBSOLETE"),
        (completed_path, "LC-COMPLETED"),
        (archived_path, "LC-ARCHIVED"),
        (open_path, "LC-OPEN"),
    ):
        path.write_text(path.read_text(encoding="utf-8").replace("slice: B1", f"slice: {slice_id}"), encoding="utf-8")

    planspecs.mark_planspec_shipped(
        shipped_path,
        plans_root=plans_root,
        author="tester",
        kanban_state="completed",
        kanban_root_task_id="t_ship1234",
    )
    planspecs.mark_planspec_not_needed(obsolete_path, plans_root=plans_root, author="tester")
    completed = planspecs.ingest_planspec(completed_path, plans_root=plans_root)
    archived = planspecs.ingest_planspec(archived_path, plans_root=plans_root)
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.executemany(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                [(task_id,) for task_id in completed["child_ids"]],
            )
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (completed["root_task_id"],))
        assert kb.archive_task(conn, archived["root_task_id"]) is True

    open_records = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)
    all_records = planspecs.list_planspecs(plans_root=plans_root, scope="all", include_kanban_status=True)
    by_filename = {row["filename"]: row for row in all_records}

    assert [row["filename"] for row in open_records] == [open_path.name]
    assert by_filename[shipped_path.name]["open"] is False
    assert by_filename[shipped_path.name]["closed_reason"] == "closed status: shipped"
    assert by_filename[shipped_path.name]["kanban_state"] == "not_ingested"
    assert by_filename[shipped_path.name]["kanban_root_task_id"] == "t_ship1234"
    assert by_filename[obsolete_path.name]["open"] is False
    assert by_filename[obsolete_path.name]["closed_reason"] == "closed status: obsolete"
    assert by_filename[completed_path.name]["open"] is False
    assert by_filename[completed_path.name]["closed_reason"] == "kanban state: completed"
    assert by_filename[completed_path.name]["kanban_state"] == "completed"
    assert by_filename[completed_path.name]["kanban_child_done"] == 2
    assert by_filename[archived_path.name]["open"] is False
    assert by_filename[archived_path.name]["closed_reason"] == "kanban state: archived"
    assert by_filename[archived_path.name]["kanban_state"] == "archived"
    assert by_filename[open_path.name]["open"] is True
    assert by_filename[open_path.name]["closed_reason"] is None
    assert by_filename[open_path.name]["kanban_state"] == "not_ingested"


def test_list_planspecs_derives_blocked_and_running_kanban_state(kanban_home, tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    result = planspecs.ingest_planspec(path, plans_root=plans_root)

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (result["child_ids"][0],))
    running_records = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)
    assert len(running_records) == 1
    running = running_records[0]
    assert running["open"] is True
    assert running["closed_reason"] is None
    assert running["kanban_state"] == "running"
    assert running["kanban_child_running"] == 1

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (result["child_ids"][1],))
    blocked_records = planspecs.list_planspecs(plans_root=plans_root, include_kanban_status=True)
    assert len(blocked_records) == 1
    blocked = blocked_records[0]
    assert blocked["open"] is True
    assert blocked["closed_reason"] is None
    assert blocked["kanban_state"] == "blocked"
    assert blocked["kanban_child_blocked"] == 1
    assert blocked["kanban_child_running"] == 1


def test_list_planspecs_does_not_reopen_terminal_legacy_ingest_on_other_board(
    kanban_home, tmp_path: Path
):
    """A terminal default ingest must not become health-track open work again."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    result = planspecs.ingest_planspec(path, plans_root=plans_root, board="default")
    with kb.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (result["root_task_id"],))

    kb.create_board("health-track", name="Health track")
    records = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="health-track", scope="all"
    )

    assert len(records) == 1
    assert records[0]["kanban_state"] == "completed"
    assert records[0]["open"] is False


def test_list_planspecs_only_surfaces_uningested_explicit_board_on_its_owner(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    _set_planspec_board(path, "health-track")
    kb.create_board("health-track", name="Health track")

    health = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="health-track", scope="all"
    )
    default = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="default", scope="all"
    )

    assert health[0]["kanban_state"] == "not_ingested"
    assert health[0]["open"] is True
    assert default[0]["open"] is False


def test_list_planspecs_only_surfaces_never_ingested_legacy_source_on_default(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    _write_planspec(plans_root)
    kb.create_board("health-track", name="Health track")

    default = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="default", scope="all"
    )
    health = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="health-track", scope="all"
    )

    assert default[0]["open"] is True
    assert health[0]["open"] is False


def test_list_planspecs_uses_local_state_when_same_source_is_ingested_on_two_boards(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    default = planspecs.ingest_planspec(path, plans_root=plans_root, board="default")
    kb.create_board("health-track", name="Health track")
    health = planspecs.ingest_planspec(path, plans_root=plans_root, board="health-track")
    with kb.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (default["root_task_id"],))

    default_records = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="default", scope="all"
    )
    health_records = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="health-track", scope="all"
    )

    assert default_records[0]["kanban_root_task_id"] == default["root_task_id"]
    assert default_records[0]["kanban_state"] == "completed"
    assert health_records[0]["kanban_root_task_id"] == health["root_task_id"]
    assert health_records[0]["kanban_state"] == "queued"


def test_list_planspecs_explicit_board_wins_over_old_terminal_foreign_ingest(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    default = planspecs.ingest_planspec(path, plans_root=plans_root, board="default")
    with kb.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (default["root_task_id"],))
    _set_planspec_board(path, "health-track")
    kb.create_board("health-track", name="Health track")

    health = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="health-track", scope="all"
    )
    default_records = planspecs.list_planspecs(
        plans_root=plans_root, include_kanban_status=True, board="default", scope="all"
    )

    assert health[0]["kanban_state"] == "not_ingested"
    assert health[0]["open"] is True
    assert default_records[0]["open"] is False


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


def test_ac_f6_supersede_refuses_typed_wait_before_archiving_any_chain_task(
    kanban_home, tmp_path: Path
):
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root)
    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    protected_child = first["child_ids"][0]
    with kb.connect_closing() as conn:
        waiter = kb.create_task(conn, title="waits on old PlanSpec event")
        assert kb.block_task(
            conn,
            waiter,
            kind="dependency",
            wait_for={
                "type": "event_seen",
                "task_id": protected_child,
                "event_kind": "operator_approved",
            },
        )
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("Document schema", "Document schema v2"), encoding="utf-8"
    )

    with pytest.raises(planspecs.PlanSpecBlocked) as exc:
        planspecs.ingest_planspec(path, plans_root=plans_root, supersede=True)

    assert "typed worker wait" in "\n".join(exc.value.findings)
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, first["root_task_id"]).status != "archived"
        assert all(
            kb.get_task(conn, child_id).status != "archived"
            for child_id in first["child_ids"]
        )
        assert kb.get_task(conn, waiter).wait_for["task_id"] == protected_child
        assert [r["id"] for r in _active_planspec_roots(conn)] == [
            first["root_task_id"]
        ]


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


def test_ac_w2_s5_2_supersede_crash_between_archive_and_create_auto_recovers(
    kanban_home, tmp_path: Path, monkeypatch
):
    """AC-W2-S5-2: crash after archive / before create leaves no orphan half-state.

    A durable supersede intent is auto-drained on the next recovery pass so the
    operator never needs manual board surgery to finish the create half.
    """
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root, name="2026-07-22-w2s5.md")

    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert first["ok"] is True
    old_root = first["root_task_id"]
    old_children = list(first["child_ids"])

    # Mutate content so a new idempotency key is required.
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('topic: "Planspec Hub"', 'topic: "Planspec Hub v2"'),
        encoding="utf-8",
    )

    real_create = kb.create_task
    calls = {"n": 0}

    def boom_once(conn, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash between archive and create")
        return real_create(conn, *args, **kwargs)

    monkeypatch.setattr(kb, "create_task", boom_once)
    monkeypatch.setattr(planspecs.kanban_db, "create_task", boom_once)

    with pytest.raises(RuntimeError, match="injected crash"):
        planspecs.ingest_planspec(path, plans_root=plans_root, supersede=True)

    # Half-state: old chain archived, no live root for the new content, but a
    # durable intent row must exist so recovery can finish the create.
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, old_root).status == "archived"
        for cid in old_children:
            assert kb.get_task(conn, cid).status == "archived"
        open_intents = planspecs._list_open_planspec_mutation_intents(conn)
        assert open_intents, "supersede intent must survive the crash"
        assert open_intents[0]["kind"] == "supersede"
        live_roots = _active_planspec_roots(conn)
        assert live_roots == []

    # Automatic recovery (no operator --supersede required).
    actions = None
    with kb.connect_closing() as conn:
        actions = planspecs.recover_planspec_mutation_intents(
            conn, plans_root=plans_root
        )
    assert actions and actions[0]["action"] == "recovered"
    new_root = actions[0]["root_task_id"]
    assert new_root and new_root != old_root

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, old_root).status == "archived"
        assert kb.get_task(conn, new_root) is not None
        assert kb.get_task(conn, new_root).status != "archived"
        assert planspecs._list_open_planspec_mutation_intents(conn) == []
        roots = _active_planspec_roots(conn)
        assert [r["id"] for r in roots] == [new_root]


def test_ac_w2_s5_2_happy_path_supersede_clears_intent(kanban_home, tmp_path: Path):
    """AC-W2-S5-3 adjacent: normal supersede leaves no sticky intent row."""
    plans_root = tmp_path / "03-Agents"
    path = _write_planspec(plans_root, name="2026-07-22-w2s5-happy.md")
    first = planspecs.ingest_planspec(path, plans_root=plans_root)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('topic: "Planspec Hub"', 'topic: "Planspec Hub happy"'),
        encoding="utf-8",
    )
    second = planspecs.ingest_planspec(path, plans_root=plans_root, supersede=True)
    assert second["ok"] is True
    assert first["root_task_id"] in second["superseded"]
    with kb.connect_closing() as conn:
        assert planspecs._list_open_planspec_mutation_intents(conn) == []
        assert kb.get_task(conn, first["root_task_id"]).status == "archived"
        assert kb.get_task(conn, second["root_task_id"]).status != "archived"
