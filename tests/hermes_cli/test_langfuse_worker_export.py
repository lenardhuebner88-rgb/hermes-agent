from __future__ import annotations

import json
import sqlite3

import hermes_cli.langfuse_worker_export as exporter
from hermes_cli.usage_facts_db import upsert_run_facts


def _row(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM run_usage_facts WHERE run_id='claude-run-1'"
        ).fetchone()


def _seed(path) -> None:
    upsert_run_facts(
        "claude-run-1",
        {
            "origin": "claude_code",
            "task_run_id": "42",
            "task_id": "task-42",
            "chain_id": "chain-42",
            "board": "default",
            "session_id": "session-42",
            "correlation_source": "claude_session_id",
            "provider": "anthropic",
            "model": "claude-opus",
            "profile": "critic",
            "input_tokens": 100,
            "output_tokens": 25,
            "cache_read_tokens": 50,
            "cache_write_tokens": 5,
            "captured_at": "2026-07-29T00:00:00+00:00",
            "source": "measured",
        },
        path=path,
    )
    upsert_run_facts(
        "live-hermes-run",
        {
            "origin": "hermes_agent",
            "task_run_id": "43",
            "task_id": "task-43",
            "correlation_source": "kanban_runtime",
            "captured_at": "2026-07-29T00:00:00+00:00",
        },
        path=path,
    )


def test_events_are_deterministic_metadata_only_and_session_grouped(tmp_path) -> None:
    path = tmp_path / "usage.db"
    _seed(path)
    row = _row(path)

    first = exporter.events_for_row(row)
    second = exporter.events_for_row(row)

    assert first == second
    rendered = json.dumps(first)
    assert "prompt" not in rendered.lower()
    assert "tool_args" not in rendered
    trace = first[0]["body"]
    generation = first[1]["body"]
    assert trace["sessionId"] == "chain-42"
    assert trace["environment"] == generation["environment"] == "usage-facts-backfill"
    assert "kanban-worker-usage" in trace["tags"]
    assert "kanban-worker" not in trace["tags"]
    assert trace["metadata"]["task_run_id"] == "42"
    assert trace["metadata"]["kanban_task_id"] == "task-42"
    assert trace["metadata"]["worker_usage_backfill"] is True
    assert generation["metadata"]["kanban_task_id"] == "task-42"
    assert generation["model"] == "claude-opus"
    assert generation["usageDetails"] == {
        "input": 100,
        "output": 25,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 5,
        "total": 180,
    }


def test_generation_exports_measured_end_time_and_ttft(tmp_path) -> None:
    path = tmp_path / "usage.db"
    _seed(path)
    upsert_run_facts(
        "claude-run-1",
        {
            "duration_ms": 2500,
            "first_token_ms": 350,
            "captured_at": "2026-07-29T00:00:00+00:00",
        },
        path=path,
    )

    generation = exporter.events_for_row(_row(path))[1]["body"]

    assert generation["startTime"] == "2026-07-29T00:00:00Z"
    assert generation["completionStartTime"] == "2026-07-29T00:00:00.350000Z"
    assert generation["endTime"] == "2026-07-29T00:00:02.500000Z"


def test_dry_run_needs_no_credentials_and_excludes_live_hermes(tmp_path) -> None:
    path = tmp_path / "usage.db"
    _seed(path)

    report = exporter.export_worker_facts(
        usage_path=path,
        env={},
        dry_run=True,
        run_limit=None,
        ledger_path=tmp_path / "ledger.json",
    )

    assert report["exact_correlated_runs"] == 2
    assert report["eligible_foreign_runs"] == 1
    assert report["skipped_live_origins"] == 1
    assert report["selected_runs"] == 1
    assert report["would_post_events"] == 2


def test_successful_batches_checkpoint_the_ledger(monkeypatch, tmp_path) -> None:
    path = tmp_path / "usage.db"
    ledger = tmp_path / "ledger.json"
    _seed(path)
    calls = []
    monkeypatch.setattr(
        exporter, "_credentials", lambda _env: ("http://langfuse", "Basic x")
    )
    monkeypatch.setattr(
        exporter,
        "_request_with_retry",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )

    first = exporter.export_worker_facts(
        usage_path=path,
        env={},
        dry_run=False,
        run_limit=None,
        ledger_path=ledger,
    )
    second = exporter.export_worker_facts(
        usage_path=path,
        env={},
        dry_run=False,
        run_limit=None,
        ledger_path=ledger,
    )

    assert first["posted_runs"] == 1
    assert first["posted_events"] == 2
    assert second["posted_runs"] == 0
    assert len(calls) == 1
    posted = calls[0][1]["payload"]["batch"]
    assert all(
        event["body"].get("metadata", {}).get("origin") != "hermes_agent"
        for event in posted
    )
    assert json.loads(ledger.read_text())["exported_run_ids"] == ["claude-run-1"]


def test_explicit_empty_environment_never_falls_back_to_process_env(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "usage.db"
    _seed(path)
    received = []
    monkeypatch.setattr(
        exporter,
        "_credentials",
        lambda selected: (
            received.append(selected)
            or (_ for _ in ()).throw(RuntimeError("missing credentials"))
        ),
    )

    try:
        exporter.export_worker_facts(
            usage_path=path,
            env={},
            ledger_path=tmp_path / "ledger.json",
        )
    except RuntimeError as exc:
        assert "missing credentials" in str(exc)
    else:
        raise AssertionError("an explicit empty environment must fail closed")

    assert received == [{}]


def test_ingestion_event_errors_do_not_advance_ledger(monkeypatch, tmp_path) -> None:
    path = tmp_path / "usage.db"
    ledger = tmp_path / "ledger.json"
    _seed(path)
    monkeypatch.setattr(
        exporter, "_credentials", lambda _env: ("http://langfuse", "Basic x")
    )
    monkeypatch.setattr(
        exporter,
        "_request_with_retry",
        lambda *args, **kwargs: {"errors": [{"status": 400}]},
    )

    try:
        exporter.export_worker_facts(
            usage_path=path,
            env={},
            ledger_path=ledger,
        )
    except RuntimeError as exc:
        assert "rejected one or more worker usage events" in str(exc)
    else:
        raise AssertionError("partial ingestion errors must fail closed")

    assert not ledger.exists()


def test_row_selection_retains_only_the_requested_canary(tmp_path) -> None:
    path = tmp_path / "usage.db"
    _seed(path)
    for index in range(2, 12):
        upsert_run_facts(
            f"claude-run-{index}",
            {
                "origin": "claude_code",
                "task_id": f"task-{index}",
                "correlation_source": "claude_session_id_task",
                "captured_at": f"2026-07-29T00:00:{index:02d}+00:00",
            },
            path=path,
        )

    snapshot = exporter._eligibility_snapshot(
        path,
        exported={"claude-run-2"},
        run_limit=3,
    )

    assert len(snapshot.selected_rows) == 3
    assert snapshot.eligible_foreign_runs == 11
    assert snapshot.pending_runs == 10


def test_invalid_timestamp_is_counted_without_blocking_the_batch(tmp_path) -> None:
    path = tmp_path / "usage.db"
    _seed(path)
    upsert_run_facts(
        "claude-invalid-time",
        {
            "origin": "claude_code",
            "task_id": "task-invalid",
            "session_id": "session-invalid",
            "correlation_source": "claude_session_id_task",
            "captured_at": "not-a-timestamp",
        },
        path=path,
    )

    report = exporter.export_worker_facts(
        usage_path=path,
        env={},
        dry_run=True,
        run_limit=None,
        ledger_path=tmp_path / "ledger.json",
    )

    assert report["invalid_timestamp_runs"] == 1
    assert report["selected_runs"] == 1
    assert report["would_post_events"] == 2


def test_malformed_ledger_fails_closed_before_credentials(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "usage.db"
    ledger = tmp_path / "ledger.json"
    _seed(path)
    ledger.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        exporter,
        "_credentials",
        lambda _env: (_ for _ in ()).throw(
            AssertionError("must not request credentials")
        ),
    )

    try:
        exporter.export_worker_facts(
            usage_path=path,
            env={},
            dry_run=False,
            run_limit=None,
            ledger_path=ledger,
        )
    except RuntimeError as exc:
        assert "ledger is unreadable" in str(exc)
    else:
        raise AssertionError("a malformed ledger must fail closed")
