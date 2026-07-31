from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent.usage_pricing import CanonicalUsage, CostResult
from hermes_cli import kanban_db as kb
from hermes_cli import usage_facts_readmodel
from hermes_cli.fleet_metrics_readmodel import build_fleet_metrics_payload
from hermes_cli.usage_facts_db import upsert_run_facts


def _fake_equivalent(
    model_name: str,
    usage: CanonicalUsage,
    *,
    provider: str | None = None,
    **_kwargs: Any,
) -> CostResult:
    amount = Decimal(
        usage.input_tokens
        + usage.output_tokens
        + usage.cache_read_tokens
        + usage.cache_write_tokens
    ) / Decimal(1_000_000)
    return CostResult(
        amount_usd=amount,
        status="equivalent",
        source="user_override",
        label=f"{provider}/{model_name}",
        pricing_version="fixture-v1",
    )


def test_fleet_metrics_separates_eligible_coverage_and_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        usage_facts_readmodel,
        "estimate_equivalent_cost",
        _fake_equivalent,
    )
    kb.init_db()
    board_path = home / "kanban.db"
    usage_path = tmp_path / "usage_facts.db"
    generated_at = "2026-07-30T13:00:00+00:00"
    captured_at = "2026-07-30T12:55:00+00:00"
    now = int(datetime.fromisoformat(generated_at).timestamp())

    for index in range(1, 11):
        upsert_run_facts(
            f"hermes-{index}",
            {
                "origin": "hermes_agent",
                "task_run_id": str(index),
                "task_id": f"task-{index}",
                "chain_id": "chain-main",
                "board": "default",
                "provider": "fixture-provider",
                "model": "fixture-model",
                "billing_mode": "subscription_included",
                "input_tokens": 100 * index,
                "cache_read_tokens": 10,
                "cache_write_tokens": 0,
                "output_tokens": 5,
                "duration_ms": 1000,
                "first_token_ms": 200,
                "captured_at": captured_at,
                "source": "measured",
            },
            path=usage_path,
        )
    upsert_run_facts(
        "claude-history",
        {
            "origin": "claude_code",
            "provider": "anthropic",
            "model": "claude-test",
            "billing_mode": "subscription_included",
            "input_tokens": 200,
            "cache_read_tokens": 100,
            "cache_write_tokens": 0,
            "output_tokens": 20,
            "captured_at": captured_at,
            "source": "measured",
        },
        path=usage_path,
    )
    for index in range(2):
        upsert_run_facts(
            f"grok-{index}",
            {
                "origin": "grok_cli",
                "provider": "xai-oauth",
                "model": None,
                "billing_mode": "subscription_included",
                "input_tokens": 50,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 5,
                "duration_ms": 500,
                "first_token_ms": 100 if index == 0 else None,
                "captured_at": captured_at,
                "source": "measured",
            },
            path=usage_path,
        )

    with sqlite3.connect(board_path) as connection:
        for index in range(1, 11):
            connection.execute(
                """
                INSERT INTO tasks (
                    id, title, status, created_at, workspace_kind
                ) VALUES (?, ?, 'done', ?, 'scratch')
                """,
                (f"task-{index}", f"Task {index}", now - 120),
            )
            connection.execute(
                """
                INSERT INTO task_runs (
                    id, task_id, profile, status, started_at, ended_at,
                    metadata
                ) VALUES (?, ?, 'coder', 'done', ?, ?, ?)
                """,
                (
                    index,
                    f"task-{index}",
                    now - 100,
                    now - 10,
                    json.dumps({"chain_id": "chain-main"}),
                ),
            )
            connection.executemany(
                """
                INSERT INTO worker_run_timeline_events (
                    task_run_id, event_kind, observed_at_ms, source,
                    task_id, board, chain_root_id, profile
                ) VALUES (?, ?, ?, 'test', ?, 'default', 'chain-main', 'coder')
                """,
                (
                    (
                        index,
                        "queued",
                        (now - 102) * 1000,
                        f"task-{index}",
                    ),
                    (
                        index,
                        "claimed",
                        (now - 100) * 1000,
                        f"task-{index}",
                    ),
                ),
            )
        connection.executemany(
            """
            INSERT INTO worker_run_retry_links (
                task_run_id, retry_of_task_run_id, retry_class,
                triggering_event_id, task_id, board
            ) VALUES (?, ?, 'auto', ?, ?, 'default')
            """,
            (
                (9, 7, 900, "task-9"),
                (10, 8, 901, "task-10"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO scores (
                run_id, task_id, name, value, value_type, source, created_at
            ) VALUES (1, 'task-1', 'review_verdict', ?, 'binary',
                      'review_gate', ?)
            """,
            ((0.0, now - 50), (1.0, now - 40)),
        )
        connection.execute(
            """
            INSERT INTO tasks (
                id, title, status, created_at, workspace_kind
            ) VALUES (
                'queued-ready', 'Queued ready', 'scheduled', ?, 'scratch'
            )
            """,
            (now - 60,),
        )

    sentinel_path = tmp_path / "sentinel-status.json"
    sentinel_path.write_text(
        json.dumps(
            {
                "contract_version": "observability-sentinel.v1",
                "status": "passed",
                "checked_at": generated_at,
                "last_success_at": generated_at,
                "task_run_id": 42,
            }
        ),
        encoding="utf-8",
    )

    payload = build_fleet_metrics_payload(
        usage_path,
        board_path,
        days=7,
        generated_at=generated_at,
        sentinel_status_path=sentinel_path,
    )

    coverage = payload["provider_model_coverage"]["coverage"]
    assert coverage["model"]["all_sources"]["observed_rows"] == 11
    assert coverage["model"]["all_sources"]["denominator_rows"] == 13
    assert coverage["model"]["eligible_sources"]["observed_rows"] == 11
    assert coverage["model"]["eligible_sources"]["denominator_rows"] == 11
    assert coverage["model"]["eligible_sources"]["status"] == "complete"
    assert coverage["duration"]["all_sources"]["observed_rows"] == 12
    assert coverage["duration"]["all_sources"]["denominator_rows"] == 13
    assert coverage["duration"]["eligible_sources"]["status"] == "complete"
    assert coverage["duration"]["eligible_sources"]["denominator_rows"] == 12
    assert coverage["ttft"]["eligible_sources"]["observed_rows"] == 11
    assert coverage["ttft"]["eligible_sources"]["denominator_rows"] == 12

    assert payload["usage"]["tasks"]["total_buckets"] == 10
    assert payload["usage"]["chains"]["total_buckets"] == 1
    assert payload["reliability"]["retries"]["retry_rate"] == pytest.approx(0.2)
    assert payload["alerts"]["retry_spike"]["status"] == "warning"
    assert payload["reliability"]["queue"]["eligible_backlog"] == 1
    assert payload["reliability"]["queue"]["queue_wait_ms"]["p95"] == 2000
    assert payload["alerts"]["queue_congestion"]["status"] == "ok"
    assert payload["quality"]["reviews"]["approvals"] == 1
    assert payload["quality"]["reviews"]["request_changes"] == 1
    assert payload["quality"]["reviews"]["rework_rounds"] == 1
    assert payload["quality"]["reviews"]["approval_rate_unit"] == (
        "verdict_rounds"
    )
    assert payload["quality"]["reviews"]["final_task_approval_rate"] == 1.0
    assert payload["alerts"]["data_freshness"]["status"] == "ok"
    assert payload["alerts"]["sentinel"]["status"] == "ok"
    # Cost comparisons stay fail-closed until result and execution-adoption
    # populations are available, even when their raw computation is normal.
    assert payload["alerts"]["cost_outlier"]["status"] == "unknown"
    assert payload["alerts"]["cost_outlier"]["computed_status"] == "ok"
    assert payload["usage"]["fact_coverage"]["denominator_kind"] == (
        "usage_fact_rows"
    )
    assert payload["usage"]["execution_adoption"] == {
        "observed_executions": None,
        "denominator_executions": None,
        "ratio": None,
        "status": "unknown",
        "reason": "universal_execution_denominator_unavailable",
    }


def test_missing_sentinel_and_small_samples_stay_unknown(
    tmp_path: Path,
) -> None:
    usage_path = tmp_path / "usage.db"
    board_path = tmp_path / "missing-kanban.db"
    upsert_run_facts(
        "one",
        {
            "origin": "claude_code",
            "provider": "anthropic",
            "model": "claude-test",
            "input_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 1,
            "captured_at": "2026-07-30T12:59:00+00:00",
            "source": "measured",
        },
        path=usage_path,
    )

    payload = build_fleet_metrics_payload(
        usage_path,
        board_path,
        generated_at="2026-07-30T13:00:00+00:00",
        sentinel_status_path=tmp_path / "missing-sentinel.json",
    )

    assert payload["alerts"]["sentinel"]["status"] == "unknown"
    assert payload["alerts"]["data_freshness"]["status"] == "ok"
    assert payload["alerts"]["data_freshness"]["sources"] == [
        {
            "origin": "claude_code",
            "fact_rows": 1,
            "latest_captured_at": "2026-07-30T12:59:00+00:00",
            "age_seconds": 60,
            "threshold_seconds": 7200,
            "status": "ok",
            "unknown_reason": None,
        }
    ]
    assert payload["alerts"]["retry_spike"]["status"] == "unknown"
    assert payload["alerts"]["queue_congestion"]["status"] == "unknown"
    assert payload["alerts"]["cost_outlier"]["status"] == "unknown"
    claude = payload["provider_model_coverage"]["groups"][0]
    assert (
        claude["coverage"]["ttft"]["eligible_sources"]["status"]
        == "not_applicable"
    )


def test_retry_rate_uses_only_instrumented_eligible_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    board_path = home / "kanban.db"
    usage_path = tmp_path / "usage.db"
    now = int(datetime(2026, 7, 30, 13, tzinfo=timezone.utc).timestamp())
    upsert_run_facts(
        "usage-fact",
        {
            "origin": "hermes_agent",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "input_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 1,
            "captured_at": "2026-07-30T12:59:00+00:00",
        },
        path=usage_path,
    )
    with sqlite3.connect(board_path) as connection:
        for index in range(1, 31):
            connection.execute(
                """
                INSERT INTO tasks (id, title, status, created_at, workspace_kind)
                VALUES (?, ?, 'done', ?, 'scratch')
                """,
                (f"task-{index}", f"Task {index}", now - 100),
            )
            connection.execute(
                """
                INSERT INTO task_runs (id, task_id, profile, status, started_at)
                VALUES (?, ?, 'coder', 'done', ?)
                """,
                (index, f"task-{index}", now - 90),
            )
        # Ten current runs have the lifecycle instrumentation; twenty historical
        # rows remain in-window but must not dilute the retry denominator.
        connection.executemany(
            """
            INSERT INTO worker_run_timeline_events (
                task_run_id, event_kind, observed_at_ms, source, task_id, board
            ) VALUES (?, 'queued', ?, 'test', ?, 'default')
            """,
            ((index, (now - 89) * 1000, f"task-{index}") for index in range(21, 31)),
        )
        connection.execute(
            """
            INSERT INTO worker_run_retry_links (
                task_run_id, retry_of_task_run_id, retry_class,
                triggering_event_id, task_id, board
            ) VALUES (30, 29, 'auto', 1, 'task-30', 'default')
            """
        )

    payload = build_fleet_metrics_payload(
        usage_path,
        board_path,
        generated_at="2026-07-30T13:00:00+00:00",
        sentinel_status_path=tmp_path / "missing-sentinel.json",
    )

    retries = payload["reliability"]["retries"]
    assert retries["all_runs"] == 30
    assert retries["denominator_runs"] == 10
    assert retries["retry_runs"] == 1
    assert retries["retry_rate"] == pytest.approx(0.1)
    assert retries["instrumentation_adoption"] == {
        "observed_runs": 10,
        "denominator_runs": 30,
        "ratio": pytest.approx(1 / 3),
        "status": "partial",
        "reason": "pre_instrumentation_or_uninstrumented_runs_present",
    }


def test_cost_outlier_uses_full_population_not_display_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_path = tmp_path / "usage.db"
    monkeypatch.setattr(
        usage_facts_readmodel,
        "estimate_equivalent_cost",
        _fake_equivalent,
    )
    captured_at = "2026-07-30T12:59:00+00:00"
    for index, input_tokens in enumerate((100, 100, 100, 100, 100, 100_000), 1):
        upsert_run_facts(
            f"run-{index}",
            {
                "origin": "hermes_agent",
                "task_run_id": str(index),
                "task_id": f"task-{index}",
                "chain_id": "chain-a",
                "board": "default",
                "provider": "fixture-provider",
                "model": "fixture-model",
                "billing_mode": "subscription_included",
                "input_tokens": input_tokens,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "captured_at": captured_at,
            },
            path=usage_path,
        )

    kwargs = {
        "generated_at": "2026-07-30T13:00:00+00:00",
        "sentinel_status_path": tmp_path / "missing-sentinel.json",
    }
    limited = build_fleet_metrics_payload(
        usage_path, tmp_path / "missing-board.db", bucket_limit=1, **kwargs
    )
    complete = build_fleet_metrics_payload(
        usage_path, tmp_path / "missing-board.db", bucket_limit=500, **kwargs
    )

    first = limited["alerts"]["cost_outlier"]
    second = complete["alerts"]["cost_outlier"]
    assert limited["usage"]["tasks"]["truncated"] is True
    assert complete["usage"]["tasks"]["truncated"] is False
    assert first["population_size"] == second["population_size"] == 6
    assert first["evaluable"] == second["evaluable"] == 6
    assert first["outlier_count"] == second["outlier_count"] == 1
    assert first["computed_status"] == second["computed_status"] == "warning"
    assert first["status"] == second["status"] == "unknown"


def test_freshness_is_per_origin_and_stale_cli_is_not_masked(
    tmp_path: Path,
) -> None:
    usage_path = tmp_path / "usage.db"
    for run_id, origin, captured_at in (
        ("hermes", "hermes_agent", "2026-07-30T12:59:00+00:00"),
        ("claude", "claude_code", "2026-07-30T09:00:00+00:00"),
    ):
        upsert_run_facts(
            run_id,
            {
                "origin": origin,
                "provider": "fixture-provider",
                "model": "fixture-model",
                "input_tokens": 1,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 1,
                "captured_at": captured_at,
            },
            path=usage_path,
        )

    payload = build_fleet_metrics_payload(
        usage_path,
        tmp_path / "missing-board.db",
        generated_at="2026-07-30T13:00:00+00:00",
        sentinel_status_path=tmp_path / "missing-sentinel.json",
    )

    freshness = payload["alerts"]["data_freshness"]
    by_origin = {item["origin"]: item for item in freshness["sources"]}
    assert freshness["status"] == "warning"
    assert by_origin["hermes_agent"]["status"] == "ok"
    assert by_origin["claude_code"]["status"] == "warning"
    assert by_origin["claude_code"]["age_seconds"] == 14_400
    assert by_origin["claude_code"]["unknown_reason"] is None
