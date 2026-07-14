"""Run-bound model-route truth for kanban workers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    profile_home = home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "worker_runtime: hermes\n"
        "model:\n"
        "  provider: openai-codex\n"
        "  name: gpt-5.6-sol\n",
        encoding="utf-8",
    )
    db_path = home / "kanban" / "boards" / "default" / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conn = kb.connect(db_path=db_path)
    try:
        yield conn, profile_home
    finally:
        conn.close()


def _ready_task(conn, *, model_override: str | None = None) -> str:
    task_id = kb.create_task(conn, title="route truth", assignee="coder")
    if model_override:
        kb.set_task_model_override(conn, task_id, model_override)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    conn.commit()
    return task_id


def _run_row(conn, task_id: str):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def test_claim_stamps_concrete_hermes_profile_route(route_db):
    conn, _ = route_db
    task_id = _ready_task(conn)

    claimed = kb.claim_task(conn, task_id, claimer="test:profile")

    assert claimed is not None
    run = _run_row(conn, task_id)
    assert run["requested_provider"] == "openai-codex"
    assert run["requested_model"] == "gpt-5.6-sol"
    assert run["active_provider"] == "openai-codex"
    assert run["active_model"] == "gpt-5.6-sol"
    assert run["model_state"] == "planned"
    assert run["model_source"] == "profile"
    assert run["model_observed_at"] is not None


def test_task_override_beats_lane_and_profile_at_claim(route_db):
    conn, _ = route_db
    lane = kb.create_lane(
        conn,
        name="lane-a",
        profiles={
            "coder": {
                "worker_runtime": "hermes",
                "provider": "openrouter",
                "model": "lane/model-a",
            }
        },
    )
    kb.activate_lane(conn, lane["id"])
    task_id = _ready_task(conn, model_override="override/model")

    kb.claim_task(conn, task_id, claimer="test:override")

    run = _run_row(conn, task_id)
    assert run["requested_provider"] == "openai-codex"
    assert run["requested_model"] == "override/model"
    assert run["model_source"] == "task_override"


def test_lane_beats_profile_and_later_lane_change_cannot_rewrite_run(route_db):
    conn, _ = route_db
    lane_a = kb.create_lane(
        conn,
        name="lane-a",
        profiles={
            "coder": {
                "worker_runtime": "hermes",
                "provider": "openrouter",
                "model": "lane/model-a",
            }
        },
    )
    kb.activate_lane(conn, lane_a["id"])
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id, claimer="test:lane")
    assert claimed is not None

    lane_b = kb.create_lane(
        conn,
        name="lane-b",
        profiles={
            "coder": {
                "worker_runtime": "hermes",
                "provider": "changed-provider",
                "model": "lane/model-b",
            }
        },
    )
    kb.activate_lane(conn, lane_b["id"])

    run = _run_row(conn, task_id)
    assert run["requested_provider"] == "openrouter"
    assert run["requested_model"] == "lane/model-a"
    assert run["active_model"] == "lane/model-a"
    assert run["model_source"] == "lane"


def test_provider_only_lane_combines_with_profile_model(route_db):
    conn, _ = route_db
    lane = kb.create_lane(
        conn,
        name="provider-only",
        profiles={
            "coder": {
                "worker_runtime": "hermes",
                "provider": "openrouter",
                "model": None,
            }
        },
    )
    kb.activate_lane(conn, lane["id"])
    task_id = _ready_task(conn)

    kb.claim_task(conn, task_id, claimer="test:provider-only")

    run = _run_row(conn, task_id)
    assert run["requested_provider"] == "openrouter"
    assert run["requested_model"] == "gpt-5.6-sol"
    assert run["model_source"] == "lane"


def test_actual_spawn_uses_claimed_route_after_lane_changes(route_db, monkeypatch):
    conn, profile_home = route_db
    lane_a = kb.create_lane(
        conn,
        name="lane-a",
        profiles={
            "coder": {
                "worker_runtime": "hermes",
                "provider": "openrouter",
                "model": "lane/model-a",
            }
        },
    )
    kb.activate_lane(conn, lane_a["id"])
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id, claimer="test:frozen-spawn")
    assert claimed is not None

    lane_b = kb.create_lane(
        conn,
        name="lane-b",
        profiles={
            "coder": {
                "worker_runtime": "hermes",
                "provider": "changed-provider",
                "model": "lane/model-b",
            }
        },
    )
    kb.activate_lane(conn, lane_b["id"])
    captured: dict[str, list[str]] = {}

    class _FakePopen:
        pid = 4242

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    assert kb._default_spawn(claimed, str(profile_home)) == 4242
    cmd = captured["cmd"]
    assert cmd[cmd.index("-m") + 1] == "lane/model-a"
    assert cmd[cmd.index("--provider") + 1] == "openrouter"
    assert "lane/model-b" not in cmd
    assert "changed-provider" not in cmd


def test_model_route_writer_tracks_fallback_once_and_deduplicates_followups(route_db):
    conn, _ = route_db
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id, claimer="test:fallback")
    assert claimed is not None and claimed.current_run_id is not None
    run_id = int(claimed.current_run_id)

    assert kb.update_run_model_route(
        conn,
        task_id,
        run_id,
        provider="openai-codex",
        model="gpt-5.6-sol",
        state="in_flight",
        source="runtime_request",
        observed_at=100,
        api_request_id="api-1",
    )
    assert kb.update_run_model_route(
        conn,
        task_id,
        run_id,
        provider="openai-codex",
        model="gpt-5.6-sol-20260713",
        state="confirmed",
        source="provider_response",
        observed_at=101,
        api_request_id="api-1",
    )
    assert kb.update_run_model_route(
        conn,
        task_id,
        run_id,
        provider="kimi-coding",
        model="kimi-k2.7-code",
        state="in_flight",
        source="runtime_request",
        observed_at=102,
        api_request_id="api-2",
    )
    assert kb.update_run_model_route(
        conn,
        task_id,
        run_id,
        provider="kimi-coding",
        model="kimi-k2.7-code",
        state="confirmed",
        source="provider_response",
        observed_at=103,
        api_request_id="api-2",
    )
    # A second request on the same route may update state/time, but must not
    # create another route-change or confirmation event for the same route.
    assert kb.update_run_model_route(
        conn,
        task_id,
        run_id,
        provider="kimi-coding",
        model="kimi-k2.7-code",
        state="in_flight",
        source="runtime_request",
        observed_at=104,
        api_request_id="api-3",
    )
    assert kb.update_run_model_route(
        conn,
        task_id,
        run_id,
        provider="kimi-coding",
        model="kimi-k2.7-code",
        state="confirmed",
        source="provider_response",
        observed_at=105,
        api_request_id="api-3",
    )

    row = _run_row(conn, task_id)
    assert (row["active_provider"], row["active_model"], row["model_state"]) == (
        "kimi-coding",
        "kimi-k2.7-code",
        "confirmed",
    )
    events = conn.execute(
        "SELECT kind, payload FROM task_events "
        "WHERE task_id = ? AND kind IN ('model_route_changed', 'model_confirmed') "
        "ORDER BY id",
        (task_id,),
    ).fetchall()
    route_changes = [json.loads(e["payload"]) for e in events if e["kind"] == "model_route_changed"]
    confirmations = [json.loads(e["payload"]) for e in events if e["kind"] == "model_confirmed"]
    # response.model may refine the primary model once, then fallback changes
    # it once. The fallback itself is represented by exactly one change event.
    fallback_changes = [e for e in route_changes if e["new"]["provider"] == "kimi-coding"]
    assert len(fallback_changes) == 1
    assert fallback_changes[0]["old"]["provider"] == "openai-codex"
    primary_in_flight = [
        e
        for e in route_changes
        if e["new"] == {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "state": "in_flight",
        }
    ]
    assert len(primary_in_flight) == 1
    assert len([e for e in confirmations if e["new"]["provider"] == "kimi-coding"]) == 1


def test_refined_provider_model_does_not_oscillate_or_flood_across_turns(route_db):
    conn, _ = route_db
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id, claimer="test:refined-followups")
    assert claimed is not None and claimed.current_run_id is not None
    run_id = int(claimed.current_run_id)

    route_event_counts: list[int] = []
    for turn in range(1, 4):
        assert kb.update_run_model_route(
            conn,
            task_id,
            run_id,
            provider="openai-codex",
            model="gpt-5.6-sol",
            state="in_flight",
            source="runtime_request",
            observed_at=200 + (turn * 2),
            api_request_id=f"api-{turn}",
        )
        assert kb.update_run_model_route(
            conn,
            task_id,
            run_id,
            provider="openai-codex",
            model="gpt-5.6-sol-20260713",
            state="confirmed",
            source="provider_response",
            observed_at=201 + (turn * 2),
            api_request_id=f"api-{turn}",
        )
        route_event_counts.append(
            conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id = ? AND run_id = ? AND kind = 'model_route_changed'",
                (task_id, run_id),
            ).fetchone()[0]
        )

    run = _run_row(conn, task_id)
    assert (run["active_provider"], run["active_model"], run["model_state"]) == (
        "openai-codex",
        "gpt-5.6-sol-20260713",
        "confirmed",
    )
    # First request records planned -> in-flight. The second request records
    # the first in-flight transition for the refined identity. Later identical
    # requests are fully deduplicated instead of appending two events forever.
    assert route_event_counts == [1, 2, 2]
    confirmations = conn.execute(
        "SELECT COUNT(*) FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'model_confirmed'",
        (task_id, run_id),
    ).fetchone()[0]
    assert confirmations == 1


def test_model_route_writer_rejects_stale_run_id(route_db):
    conn, _ = route_db
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id, claimer="test:stale")
    assert claimed is not None and claimed.current_run_id is not None
    stale_id = int(claimed.current_run_id)
    now = 200
    newer = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, "
        "requested_provider, requested_model, active_provider, active_model, "
        "model_state, model_source, model_observed_at) "
        "VALUES (?, 'coder', 'running', ?, 'new-provider', 'new-model', "
        "'new-provider', 'new-model', 'planned', 'profile', ?)",
        (task_id, now, now),
    ).lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (newer, task_id))
    conn.commit()

    assert not kb.update_run_model_route(
        conn,
        task_id,
        stale_id,
        provider="stale-provider",
        model="stale-model",
        state="confirmed",
        source="provider_response",
        observed_at=201,
    )
    stale = conn.execute("SELECT active_model FROM task_runs WHERE id = ?", (stale_id,)).fetchone()
    current = conn.execute("SELECT active_model FROM task_runs WHERE id = ?", (newer,)).fetchone()
    assert stale["active_model"] == "gpt-5.6-sol"
    assert current["active_model"] == "new-model"


def test_claude_cli_requires_and_stamps_explicit_model(route_db):
    conn, profile_home = route_db
    (profile_home / "config.yaml").write_text(
        "worker_runtime: claude-cli\nclaude_model: claude-sonnet-4-6\n",
        encoding="utf-8",
    )
    task_id = _ready_task(conn)

    kb.claim_task(conn, task_id, claimer="test:claude")

    run = _run_row(conn, task_id)
    assert run["requested_provider"] == "claude-cli"
    assert run["requested_model"] == "claude-sonnet-4-6"
    assert run["model_state"] == "planned"


def test_claude_cli_spawn_uses_the_claimed_explicit_model(route_db, monkeypatch):
    conn, profile_home = route_db
    (profile_home / "config.yaml").write_text(
        "worker_runtime: claude-cli\nclaude_model: claude-opus-4-8\n",
        encoding="utf-8",
    )
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id, claimer="test:claude-spawn")
    assert claimed is not None
    captured: dict[str, list[str]] = {}

    class _FakePopen:
        pid = 4848

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")
    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    assert kb._default_spawn(claimed, str(profile_home)) == 4848
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/local/bin/claude-test"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"


@pytest.mark.parametrize(
    "profile_config",
    [
        "worker_runtime: hermes\nmodel: {}\n",
        "worker_runtime: claude-cli\n",
    ],
    ids=["hermes", "claude-cli"],
)
def test_dispatcher_spawn_without_concrete_model_fails_closed(
    route_db, monkeypatch, profile_config
):
    conn, profile_home = route_db
    (profile_home / "config.yaml").write_text(profile_config, encoding="utf-8")
    task_id = _ready_task(conn)
    claimed = kb.claim_task(conn, task_id, claimer="test:unknown")
    assert claimed is not None

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("must not spawn"))
    with pytest.raises(RuntimeError, match="concrete model"):
        kb._default_spawn(claimed, str(Path.cwd()))


def test_dispatcher_blocks_unresolvable_model_route_without_transient_retries(
    route_db, monkeypatch
):
    conn, profile_home = route_db
    (profile_home / "config.yaml").write_text(
        "worker_runtime: hermes\nmodel: {}\n",
        encoding="utf-8",
    )
    task_id = _ready_task(conn)
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    result = kb.dispatch_once(
        conn,
        max_spawn=1,
        failure_limit=9,
        serialize_by_repo=False,
        board="default",
    )

    task = conn.execute(
        "SELECT status, transient_retry_count, last_failure_error "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    run = _run_row(conn, task_id)
    assert result.auto_blocked == [task_id]
    assert task["status"] == "blocked"
    assert task["transient_retry_count"] == 0
    assert "concrete model route" in task["last_failure_error"]
    assert run["status"] == "gave_up"
    assert run["outcome"] == "gave_up"


def test_legacy_session_model_backfill_batches_by_profile_state_db(route_db):
    _, profile_home = route_db
    state_conn = sqlite3.connect(profile_home / "state.db")
    try:
        state_conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, model TEXT, billing_provider TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER)"
        )
        state_conn.executemany(
            "INSERT INTO sessions "
            "(id, model, billing_provider, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("session-a", "model-a", "provider-a", 10, 2),
                ("session-b", "model-b", "provider-b", 20, 4),
            ],
        )
        state_conn.commit()
    finally:
        state_conn.close()

    usage = kb._backfill_usage_batch_from_state_db(
        [("session-a", "coder"), ("session-b", "coder")]
    )

    assert usage[("session-a", "coder")]["model"] == "model-a"
    assert usage[("session-a", "coder")]["billing_provider"] == "provider-a"
    assert usage[("session-b", "coder")]["input_tokens"] == 20
