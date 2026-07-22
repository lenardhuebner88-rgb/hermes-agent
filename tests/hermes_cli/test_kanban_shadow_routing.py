from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_shadow_routing as shadow


@pytest.fixture
def conn(tmp_path):
    connection = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield connection
    finally:
        connection.close()


def _manifest(*, phase: str = "execute", profile: str = "worker_slim") -> dict:
    return {
        "audience": "hermes",
        "chars": 1234,
        "omitted_records": 0,
        "payload_fingerprint": "f" * 64,
        "phase": phase,
        "profile": profile,
        "renderer_version": "worker-brief-v1",
        "section_counts": {"assignment": {"available": 1, "included": 1, "omitted": 0}},
        "token_estimate": 321,
    }


def _seed_completed(
    conn,
    index: int,
    *,
    provider: str,
    model: str,
    total_tokens: int,
    phase: str = "execute",
    profile: str = "worker_slim",
    review_tier: str = "standard",
    workspace_kind: str = "worktree",
    title: str = "implement feature",
    outcome: str = "completed",
    include_tokens: bool = True,
    complete_manifest: bool = True,
) -> None:
    task_id = f"done-{index}"
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, status, kind, review_tier, workspace_kind, created_by, created_at, completed_at) "
        "VALUES (?, ?, 'done', 'code', ?, ?, 'operator', ?, ?)",
        (task_id, title, review_tier, workspace_kind, index, index + 10),
    )
    brief = _manifest(phase=phase, profile=profile)
    if not complete_manifest:
        brief.pop("payload_fingerprint")
    input_tokens = total_tokens - 10 if include_tokens else None
    output_tokens = 10 if include_tokens else None
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, outcome, started_at, ended_at, input_tokens, output_tokens, "
        "requested_provider, requested_model, active_provider, active_model, metadata) "
        "VALUES (?, 'done', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            outcome,
            index,
            index + 10,
            input_tokens,
            output_tokens,
            provider,
            model,
            provider,
            model,
            json.dumps({"brief": brief}),
        ),
    )


def _seed_current(conn) -> tuple[str, int]:
    task_id = "current"
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, status, kind, review_tier, workspace_kind, created_by, created_at) "
        "VALUES (?, 'implement feature', 'running', 'code', 'standard', "
        "'worktree', 'operator', 1000)",
        (task_id,),
    )
    metadata = {
        "brief": _manifest(),
        "requested_provider": "provider-a",
        "requested_model": "model-a",
        "actual_provider": "provider-a",
        "actual_model": "model-a",
    }
    cursor = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, started_at, requested_provider, requested_model, "
        "active_provider, active_model, metadata) "
        "VALUES (?, 'running', 1000, 'provider-a', 'model-a', 'provider-a', 'model-a', ?)",
        (task_id, json.dumps(metadata, sort_keys=True)),
    )
    run_id = int(cursor.lastrowid)
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    conn.commit()
    return task_id, run_id


def _route_snapshot(conn, run_id: int) -> bytes:
    row = conn.execute(
        "SELECT requested_provider, requested_model, active_provider, active_model, metadata "
        "FROM task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    metadata = json.loads(row["metadata"])
    launch_route = {
        "requested_provider": row["requested_provider"],
        "requested_model": row["requested_model"],
        "active_provider": row["active_provider"],
        "active_model": row["active_model"],
        "metadata_requested_provider": metadata["requested_provider"],
        "metadata_requested_model": metadata["requested_model"],
        "metadata_actual_provider": metadata["actual_provider"],
        "metadata_actual_model": metadata["actual_model"],
    }
    return json.dumps(launch_route, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    ("history_size", "expected_events"),
    [(29, 0), (30, 1), (31, 1), (60, 1)],
)
def test_shadow_threshold_and_exactly_once_without_route_mutation(
    conn, history_size, expected_events
):
    for index in range(history_size):
        if index >= history_size - 10:
            _seed_completed(
                conn, index, provider="provider-b", model="model-b", total_tokens=100
            )
        else:
            _seed_completed(
                conn, index, provider="provider-a", model="model-a", total_tokens=1000
            )
    task_id, run_id = _seed_current(conn)
    before = _route_snapshot(conn, run_id)

    first = shadow.record_routing_shadow_decision(
        conn,
        task_id=task_id,
        run_id=run_id,
        window=40,
        value_classifier=kb.value_class,
        now=2000,
    )
    second = shadow.record_routing_shadow_decision(
        conn,
        task_id=task_id,
        run_id=run_id,
        window=40,
        value_classifier=kb.value_class,
        now=2001,
    )

    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'routing_shadow_decision'",
        (task_id,),
    ).fetchall()
    assert len(events) == expected_events
    assert _route_snapshot(conn, run_id) == before
    if expected_events:
        payload = json.loads(events[0]["payload"])
        assert first == payload
        assert second is None
        assert payload["recommendation"] == {"provider": "provider-b", "model": "model-b"}
        assert payload["actual_route"] == {"provider": "provider-a", "model": "model-a"}
        assert payload["window_completions"] == min(history_size, 40)
        assert "cost" not in json.dumps(payload).lower()
    else:
        assert first is None
        assert second is None


def test_shadow_window_hard_caps_at_fifty_and_excludes_ineligible_rows(conn):
    for index in range(60):
        _seed_completed(
            conn,
            index,
            provider="provider-b",
            model="model-b",
            total_tokens=100 + index,
        )
    _seed_completed(
        conn, 100, provider="bad", model="infra-error", total_tokens=1,
        outcome="timed_out",
    )
    _seed_completed(
        conn, 101, provider="bad", model="missing-tokens", total_tokens=1,
        include_tokens=False,
    )
    _seed_completed(
        conn, 102, provider="bad", model="partial-brief", total_tokens=1,
        complete_manifest=False,
    )
    _seed_completed(
        conn, 103, provider="bad", model="wrong-cohort", total_tokens=1,
        profile="reviewer_review",
    )
    task_id, run_id = _seed_current(conn)

    payload = shadow.record_routing_shadow_decision(
        conn,
        task_id=task_id,
        run_id=run_id,
        window=50,
        value_classifier=kb.value_class,
        now=2000,
    )

    assert payload is not None
    assert payload["window_completions"] == 50
    assert payload["eligible_completions_seen"] == 50
    assert payload["recommendation"] == {"provider": "provider-b", "model": "model-b"}


def test_completed_run_keeps_wave2_brief_manifest(conn):
    _, run_id = _seed_current(conn)

    kb._end_run(
        conn,
        "current",
        status="done",
        outcome="completed",
        metadata={"result": "ok"},
    )

    metadata = json.loads(
        conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    )
    assert metadata["result"] == "ok"
    assert metadata["brief"] == _manifest()
