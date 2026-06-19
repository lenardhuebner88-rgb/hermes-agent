"""Unit tests for the Strategist surface read-helpers (G1).

Covers the annotation emit/parse round-trip (the I1 contract), the defensive
metrics-snapshot read (the H1 contract), and the held-operator-proposal builder
+ its root-guard against build-children.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import strategist_surface as ss


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Annotation emit/parse round-trip (I1 contract)
# ---------------------------------------------------------------------------


def test_format_then_parse_round_trips_all_fields():
    body = "Some proposal text.\n\n" + ss.format_annotation(
        target_metric="Autonomie-% von 62 → 75",
        roi="hoch — spart ~3 Eskalationen/Woche",
        counter_metric="hätte-eskalieren-sollen-Rate < 5%",
    )
    parsed = ss.parse_annotation(body)
    assert parsed["target_metric"] == "Autonomie-% von 62 → 75"
    assert parsed["roi"] == "hoch — spart ~3 Eskalationen/Woche"
    assert parsed["counter_metric"] == "hätte-eskalieren-sollen-Rate < 5%"


def test_parse_missing_block_is_all_none():
    parsed = ss.parse_annotation("A plain body with no marker at all.")
    assert parsed == {"target_metric": None, "roi": None, "counter_metric": None}


def test_parse_none_body_is_all_none():
    assert ss.parse_annotation(None) == {
        "target_metric": None,
        "roi": None,
        "counter_metric": None,
    }


def test_parse_partial_block_keeps_present_keys():
    body = "<!-- strategist-meta\ntarget_metric: nur das Ziel\n-->"
    parsed = ss.parse_annotation(body)
    assert parsed["target_metric"] == "nur das Ziel"
    assert parsed["roi"] is None
    assert parsed["counter_metric"] is None


def test_parse_accepts_json_form_and_aliases():
    body = '<!-- strategist-meta {"ziel": "X", "guardrail": "Y", "roi_estimate": "Z"} -->'
    parsed = ss.parse_annotation(body)
    assert parsed["target_metric"] == "X"
    assert parsed["counter_metric"] == "Y"
    assert parsed["roi"] == "Z"


# ---------------------------------------------------------------------------
# Metrics snapshot read (H1 contract)
# ---------------------------------------------------------------------------


def test_read_vision_metrics_missing_file_returns_none(kanban_home):
    assert ss.read_vision_metrics() is None


def test_read_vision_metrics_reads_valid_json(kanban_home):
    path = ss.vision_metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"autonomy_pct": 73, "green_gate_streak": 4}), encoding="utf-8")
    data = ss.read_vision_metrics()
    assert data == {"autonomy_pct": 73, "green_gate_streak": 4}


def test_read_vision_metrics_bad_json_returns_none(kanban_home):
    path = ss.vision_metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert ss.read_vision_metrics() is None


def test_vision_metrics_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom" / "metrics.json"
    monkeypatch.setenv("HERMES_VISION_METRICS_PATH", str(target))
    assert ss.vision_metrics_path() == target


# ---------------------------------------------------------------------------
# held_operator_proposals builder + root-guard
# ---------------------------------------------------------------------------


def _held_chain(conn, *, with_annotation: bool) -> tuple[str, str]:
    """Create a held freigabe:operator root + one held child, mirroring the
    decompose link direction (child linked as the root's parent). Returns
    (root_id, child_id)."""
    body = "Lever proposal."
    if with_annotation:
        body += "\n\n" + ss.format_annotation(
            target_metric="Ziel A", roi="hoch", counter_metric="Gegen B"
        )
    root_id = kb.create_task(conn, title="Strategist lever", body=body, assignee="coder-claude")
    child_id = kb.create_task(conn, title="Build the lever", assignee="coder-claude")
    # Mirror decompose: subtask is the parent, root is the child link target.
    kb.link_tasks(conn, parent_id=child_id, child_id=root_id)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='scheduled', freigabe='operator' WHERE id=?",
            (root_id,),
        )
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (child_id,))
    return root_id, child_id


def test_held_operator_proposals_lists_root_with_annotation(kanban_home):
    with kb.connect() as conn:
        root_id, _ = _held_chain(conn, with_annotation=True)
        proposals = ss.held_operator_proposals(conn)
    assert len(proposals) == 1
    p = proposals[0]
    assert p["id"] == root_id
    assert p["target_metric"] == "Ziel A"
    assert p["roi"] == "hoch"
    assert p["counter_metric"] == "Gegen B"
    assert p["subtask_count"] == 1


def test_held_operator_proposals_bare_root_degrades_gracefully(kanban_home):
    with kb.connect() as conn:
        _held_chain(conn, with_annotation=False)
        proposals = ss.held_operator_proposals(conn)
    assert len(proposals) == 1
    assert proposals[0]["target_metric"] is None
    assert proposals[0]["roi"] is None
    assert proposals[0]["counter_metric"] is None


def test_held_operator_proposals_excludes_children_and_non_operator(kanban_home):
    with kb.connect() as conn:
        _held_chain(conn, with_annotation=True)
        # A non-operator scheduled task must not appear.
        other = kb.create_task(conn, title="plain scheduled", assignee="coder")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (other,))
        proposals = ss.held_operator_proposals(conn)
    # Only the operator root — never the held build-child, never the plain task.
    assert len(proposals) == 1
    ids = {p["id"] for p in proposals}
    assert other not in ids
