from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from plugins.kanban.dashboard.fleet_board_readmodel import build_board_summary
from plugins.kanban.dashboard.fleet_planspec_readmodel import (
    build_readable_plan_detail,
    build_transition_preview,
    build_plan_list_response,
    classify_plan_record,
)


def _task(
    task_id: str,
    status: str,
    *,
    assignee: str | None = "coder",
    result: str | None = None,
):
    return SimpleNamespace(
        id=task_id,
        status=status,
        assignee=assignee,
        result=result,
    )


def test_board_readmodel_counts_full_scope_and_saved_views():
    tasks = [
        _task("t_1", "todo", assignee=None),
        _task("t_2", "review"),
        _task("t_3", "done", result="receipt"),
        _task("t_4", "archived", result="historical"),
    ]
    summary = build_board_summary(
        tasks,
        link_counts={
            "t_1": {"parents": 0, "children": 0},
            "t_2": {"parents": 1, "children": 0},
        },
        observed_at=123,
    )

    assert summary == {
        "total_count": 4,
        "open_count": 2,
        "status_counts": {
            "triage": 0,
            "todo": 1,
            "scheduled": 0,
            "ready": 0,
            "running": 0,
            "blocked": 0,
            "review": 1,
            "done": 1,
            "archived": 1,
        },
        "quick_counts": {
            "unassigned_open": 1,
            "review": 1,
            "standalone_open": 1,
            "with_result": 2,
        },
        "observed_at": 123,
    }


def test_plan_action_states_are_mutually_exclusive():
    ready = {
        "valid": True,
        "kanban_state": "not_ingested",
        "kanban_root_task_id": None,
        "ingest_would_block": False,
    }
    held = {
        **ready,
        "kanban_state": "queued",
        "kanban_root_task_id": "t_root",
        "freigabe": "operator",
        "kanban_root_status": "scheduled",
    }
    blocked = {
        **ready,
        "ingest_would_block": True,
        "ingest_findings": ["unknown lane: ghost"],
    }

    assert classify_plan_record(ready)[0] == "ready"
    assert classify_plan_record(held)[0] == "held"
    assert classify_plan_record(
        {**held, "kanban_root_status": "todo"}
    )[0] == "handed_off"
    assert classify_plan_record(blocked)[0] == "blocked"
    assert classify_plan_record({**ready, "status": "closed"})[0] == "completed"
    assert classify_plan_record({**ready, "status": "archived"})[0] == "archived"


def test_plan_summary_is_pre_limit(tmp_path: Path):
    records = [
        {
            "path": str(tmp_path / f"{index}.md"),
            "binding": False,
            "valid": False,
            "kanban_state": "not_ingested",
            "kanban_root_task_id": None,
            "ingest_findings": [],
            "errors": [],
        }
        for index in range(3)
    ]
    response = build_plan_list_response(
        records,
        planspecs=SimpleNamespace(),
        limit=1,
    )

    assert response["count"] == 1
    assert len(response["planspecs"]) == 1
    assert response["summary"]["draft"] == 3
    assert response["summary"]["total_matching"] == 3


def test_plan_list_limit_zero_returns_empty_but_full_summary(tmp_path: Path):
    records = [
        {
            "path": str(tmp_path / f"{index}.md"),
            "binding": False,
            "valid": False,
            "kanban_state": "not_ingested",
            "kanban_root_task_id": None,
            "ingest_findings": [],
            "errors": [],
        }
        for index in range(2)
    ]
    response = build_plan_list_response(
        records,
        planspecs=SimpleNamespace(),
        limit=0,
    )

    assert response["count"] == 0
    assert response["planspecs"] == []
    assert response["summary"]["total_matching"] == 2


def test_transition_preview_is_exact_and_write_free(tmp_path: Path):
    source = tmp_path / "binding.md"
    source.write_text("stable source", encoding="utf-8")
    spec = SimpleNamespace(
        path=source,
        board="default",
        live_test_depth="ui-real",
        children=[
            {"kind": "code", "workspace_kind": ""},
            {"kind": "research", "workspace_kind": ""},
            {"kind": "code", "workspace_kind": "worktree"},
        ],
    )
    calls: list[str] = []

    planspecs = SimpleNamespace(
        resolve_planspec_path=lambda path: Path(path),
        parse_binding_planspec=lambda path: spec,
        _collect_spec_rubric_findings=lambda parsed: [],
        _blocking_spec_rubric_findings=lambda findings: [],
        _spec_is_signed=lambda parsed: False,
        _resolve_target_board=lambda parsed, board: board or parsed.board,
        _spec_max_review_tier=lambda parsed: "critical",
        _ROLE_MISUSE_PREFIX="role_misuse: ",
    )
    kanban_db = SimpleNamespace(
        read_board_metadata=lambda board: (
            calls.append(board)
            or {"default_workdir": "/srv/hermes-agent"}
        ),
    )

    result = build_transition_preview(
        source,
        board="default",
        planspecs=planspecs,
        kanban_db=kanban_db,
    )

    assert calls == ["default"]
    assert result["action"] == "create_held_chain"
    assert result["child_tasks_created"] == 3
    assert result["starts_worker"] is False
    assert result["workspace_mode"] == "mixed"
    assert result["review_floor"] == "critical"
    assert result["auto_scout_tasks"] == 0
    assert result["can_ingest"] is True
    assert len(result["source_digest"]) == 64


def test_readable_plan_detail_keeps_closed_sources_inspectable(tmp_path: Path):
    root = tmp_path / "agents"
    path = root / "Codex" / "plans" / "closed.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        """---
status: shipped
topic: Closed but readable
freigabe: operator
live_test_depth: smoke
goal: Preserve the authored contract.
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: Verify receipt
      lane: reviewer
      deps: []
      body: Read the final evidence.
      scope_files: [RESULT.md]
---

# Closed but readable
""",
        encoding="utf-8",
    )

    from hermes_cli import planspecs as real_planspecs

    planspecs = SimpleNamespace(
        resolve_planspec_path=lambda candidate: real_planspecs.resolve_planspec_path(
            candidate, plans_root=root
        ),
        _extract_frontmatter=real_planspecs._extract_frontmatter,
        _closed_reason=real_planspecs._closed_reason,
        _first_heading=real_planspecs._first_heading,
        LIVE_TEST_DEPTHS=real_planspecs.LIVE_TEST_DEPTHS,
        PlanSpecBlocked=real_planspecs.PlanSpecBlocked,
    )

    detail = build_readable_plan_detail(path, planspecs=planspecs)

    assert detail["source_state"] == "closed"
    assert detail["goal"] == "Preserve the authored contract."
    assert detail["subtasks"][0]["body"] == "Read the final evidence."
    assert detail["subtasks"][0]["scope_files"] == ["RESULT.md"]
    assert any("closed status" in item for item in detail["source_findings"])


# --- Geschlossene PlanSpecs sind Endzustaende, keine Blocker (2026-07-29) ---
#
# Gemessen am echten Vault-Bestand: 59 von 67 roten "Blockiert"-Badges im
# Plan-Tab waren geschlossene Specs. Der Ingest-Precheck sagt fuer sie korrekt
# would_block=true ("darf ich das uebergeben?" -> nein, ist ausgeliefert), und
# genau diese richtige Antwort wurde als Alarm gerendert.

_CLOSED_BASE = {
    "valid": True,
    "binding": True,
    "kanban_state": "not_ingested",
    "kanban_root_task_id": None,
    "ingest_would_block": True,
    "ingest_findings": ["closed status: shipped"],
}


def test_shipped_planspec_is_completed_not_blocked():
    record = {**_CLOSED_BASE, "open": False, "status": "shipped",
              "closed_reason": "closed status: shipped"}
    state, reason, action = classify_plan_record(record)
    assert state == "completed", (state, reason)
    assert action == "open_result"
    assert "closed status" not in reason


def test_obsolete_planspec_is_archived_not_blocked():
    record = {**_CLOSED_BASE, "open": False, "status": "obsolete",
              "closed_reason": "closed status: obsolete",
              "ingest_findings": ["closed status: obsolete"]}
    assert classify_plan_record(record)[0] == "archived"


def test_superseded_planspec_is_archived():
    record = {**_CLOSED_BASE, "open": False, "status": "superseded",
              "closed_reason": "closed status: superseded"}
    assert classify_plan_record(record)[0] == "archived"


def test_closed_status_free_text_still_resolves_to_a_terminal_state():
    """Echte Vault-Form: der Status ist Freitext, keine Aufzaehlung traegt.

    Beide Strings stehen woertlich so im Vault (Stand 2026-07-29). Eine
    Klassifikation per Status-Menge wuerde sie still verfehlen.
    """
    shipped = {**_CLOSED_BASE, "open": False,
               "status": "SHIPPED 2026-06-19 (89527c494 — live main + FE-Build + Gateway-Restart)",
               "closed_reason": "closed status: SHIPPED 2026-06-19 (89527c494 — live main"}
    obsolete = {**_CLOSED_BASE, "open": False,
                "status": "obsolete — fix shipped+dogfood-proven 2026-06-18 (in main)",
                "closed_reason": "closed status: obsolete — fix shipped+dogfood-proven"}
    assert classify_plan_record(shipped)[0] == "completed"
    assert classify_plan_record(obsolete)[0] == "archived"


def test_closed_but_unparseable_planspec_is_not_a_draft():
    """Eine im Juni ausgelieferte, heute nicht mehr parsbare Spec ist kein Entwurf."""
    record = {"valid": False, "binding": False, "open": False, "status": "",
              "closed_reason": "closed status: shipped", "kanban_state": "not_ingested",
              "kanban_root_task_id": None, "ingest_would_block": True}
    assert classify_plan_record(record)[0] == "completed"


def test_open_planspec_with_real_finding_stays_blocked():
    """Der Schutz bleibt: ein echter Ingest-Befund ist weiterhin rot."""
    record = {**_CLOSED_BASE, "open": True, "status": "proposed", "closed_reason": None,
              "ingest_findings": ["unknown board slug: lifecycle-canary-20260714"]}
    state, reason, action = classify_plan_record(record)
    assert state == "blocked"
    assert action == "review_findings"
    assert "unknown board slug" in reason


def test_live_blocked_chain_outranks_a_closed_spec():
    """Eine laufende, blockierte Kette bleibt handlungsrelevant."""
    record = {**_CLOSED_BASE, "open": False, "status": "shipped",
              "closed_reason": "closed status: shipped",
              "kanban_state": "blocked", "kanban_root_task_id": "t_root"}
    assert classify_plan_record(record)[0] == "blocked"


def test_record_without_open_field_keeps_its_previous_classification():
    """Fehlendes ``open`` heisst NICHT geschlossen.

    ``not record.get("open")`` waere hier True und haette jeden Fixture-Record
    still als Endzustand klassifiziert -- deshalb ``is False``.
    """
    ready = {"valid": True, "kanban_state": "not_ingested",
             "kanban_root_task_id": None, "ingest_would_block": False}
    assert "open" not in ready
    assert classify_plan_record(ready)[0] == "ready"
    assert classify_plan_record({**ready, "valid": False})[0] == "draft"
    assert classify_plan_record(
        {**ready, "ingest_would_block": True, "ingest_findings": ["unknown lane: ghost"]}
    )[0] == "blocked"
