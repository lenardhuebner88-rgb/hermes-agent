"""HTTP coverage for the Hermes-only observability projection."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli.usage_facts_db import upsert_run_facts


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "observability_routes_test_plugin",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def observability_app(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    usage_path = tmp_path / "usage_facts.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_USAGE_FACTS_DB", str(usage_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    captured_at = datetime.now(timezone.utc).isoformat()
    upsert_run_facts(
        "worker-exact",
        {
            "origin": "hermes_agent",
            "task_run_id": "1",
            "task_id": "task-exact",
            "chain_id": "task-exact",
            "board": "default",
            "session_id": "session-exact",
            "correlation_source": "kanban_runtime",
            "provider": "openai",
            "model": "gpt-test",
            "profile": "coder",
            "lane": "coder",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 50,
            "cache_write_tokens": 0,
            "duration_ms": 1200,
            "first_token_ms": 250,
            "captured_at": captured_at,
            "source": "measured",
        },
        path=usage_path,
    )
    upsert_run_facts(
        "worker-unlinked",
        {
            "origin": "claude_code",
            "provider": "anthropic",
            "model": "claude-test",
            "profile": "reviewer",
            "lane": "reviewer",
            "input_tokens": 200,
            "output_tokens": 40,
            "cache_read_tokens": 100,
            "cache_write_tokens": 0,
            "captured_at": captured_at,
            "source": "measured",
        },
        path=usage_path,
    )
    # Tokenless audit rows are valid ledger entries but are outside the Usage
    # Facts read-model denominator and therefore must not skew coverage.
    upsert_run_facts(
        "worker-tokenless",
        {
            "origin": "hermes_agent",
            "task_run_id": "tokenless-run",
            "task_id": "tokenless-task",
            "model": "tokenless-model",
            "captured_at": captured_at,
            "source": "measured",
        },
        path=usage_path,
    )

    now = int(time.time())
    with kb.connect_closing() as connection:
        task_id = kb.create_task(
            connection,
            title="observability route",
            assignee="coder",
        )
        run_id = connection.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, active_model, started_at, ended_at) "
            "VALUES (?, 'coder', 'done', 'gpt-test', ?, ?)",
            (task_id, now - 120, now),
        ).lastrowid
        connection.execute(
            "INSERT INTO scores "
            "(run_id, task_id, name, value, value_type, source, created_at) "
            "VALUES (?, ?, 'run_duration_seconds', 120, 'numeric', 'test', ?)",
            (run_id, task_id, now),
        )
        fixture_task_id = kb.create_task(
            connection,
            title="synthetic observability fixture",
            assignee="w",
        )
        fixture_run_id = connection.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) "
            "VALUES (?, 'w', 'done', ?)",
            (fixture_task_id, now - 1),
        ).lastrowid
        connection.execute(
            "INSERT INTO scores "
            "(run_id, task_id, name, value, value_type, source, created_at) "
            "VALUES (?, ?, 'run_duration_seconds', 1, 'numeric', 'test', ?)",
            (fixture_run_id, fixture_task_id, now),
        )

    module = _load_plugin_module()
    monkeypatch.setattr(
        module,
        "get_langfuse_snapshot",
        lambda **_kwargs: {
            "contract_version": "langfuse-hermes-read.v1",
            "available": False,
            "state": "absent",
            "reason": "credentials_missing",
            "window_days": 7,
            "generated_at": captured_at,
            "captured_at": None,
            "cache": {"ttl_seconds": 45, "age_seconds": None},
            "metrics": {},
            "coverage": {},
            "models": [],
        },
    )
    app = FastAPI()
    app.state.observability_module = module
    app.include_router(module.router, prefix="/api/plugins/kanban")
    return TestClient(app), home / "kanban.db", usage_path


def test_observability_route_separates_stats_from_quality(observability_app):
    client, _kanban_path, _usage_path = observability_app

    response = client.get("/api/plugins/kanban/stats/observability?days=7")

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "langfuse-hermes-read.v1"
    assert payload["window_days"] == 7
    assert payload["usage"]["summary"]["fact_rows"] == 2
    assert payload["usage"]["state"] == "fresh"
    assert payload["usage"]["cache"]["ttl_seconds"] == 120
    assert payload["usage"]["cache"]["age_seconds"] >= 0
    assert payload["usage"]["coverage"]["exact_task_run_links"] == 1
    assert payload["usage"]["coverage"]["model_known"] == 2
    assert payload["usage"]["coverage"]["duration_known"] == 1
    assert payload["usage"]["coverage"]["ttft_known"] == 1
    assert payload["usage"]["coverage"]["missing_columns"] == []
    assert sum(
        row["fact_rows"] for row in payload["usage"]["daily_imports"]
    ) == payload["usage"]["summary"]["fact_rows"] == 2
    assert sum(
        row["token_rows"] for row in payload["usage"]["daily_imports"]
    ) == 2
    assert [row["name"] for row in payload["usage"]["origins"]] == [
        "claude_code",
        "hermes_agent",
    ]
    claude_origin, hermes_origin = payload["usage"]["origins"]
    assert claude_origin["coverage"]["output_tokens"] == {
        "observed_rows": 1,
        "denominator_rows": 1,
        "status": "complete",
    }
    assert claude_origin["cost"]["status"] == "unknown"
    assert hermes_origin["cost"] == {
        "known_amount_usd": None,
        "observed_facts": 0,
        "denominator_facts": 0,
        "lower_bound_facts": 0,
        "status": "unknown",
    }
    assert payload["usage"]["summary"]["cost"]["status"] == "unknown"
    assert payload["usage"]["models"][0]["coverage"]
    assert payload["run_duration"] == {
        "p50_seconds": 120.0,
        "p95_seconds": 120.0,
        "count": 1,
        "source": "kanban.metric_scores.run_duration_seconds",
    }
    assert "approval_rate" not in payload
    assert "review_verdict" not in repr(payload)


def test_warm_observability_poll_meets_live_size_budget(observability_app):
    client, _kanban_path, usage_path = observability_app
    module = client.app.state.observability_module
    target_rows = 94_682
    with sqlite3.connect(usage_path) as connection:
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(run_usage_facts)")
        ]
        quoted = ", ".join(f'"{column}"' for column in columns)
        projection = ", ".join(
            "?" if column == "run_id" else f'"{column}"' for column in columns
        )
        connection.executemany(
            f"INSERT INTO run_usage_facts ({quoted}) "
            f"SELECT {projection} FROM run_usage_facts WHERE run_id='worker-exact'",
            ((f"synthetic-{index}",) for index in range(target_rows - 2)),
        )

    warm = client.get("/api/plugins/kanban/stats/observability?days=7")
    assert warm.status_code == 200
    assert warm.json()["usage"]["summary"]["fact_rows"] == target_rows

    component_cpu_ms: dict[str, float] = {}
    for name, project in (
        ("langfuse", lambda: module.get_langfuse_snapshot(days=7)),
        (
            "usage",
            lambda: module._cached_usage_projection(
                board="default", days=7, generated_at="benchmark"
            ),
        ),
        (
            "run_duration",
            lambda: module._run_duration_projection(board="default", days=7),
        ),
    ):
        component_started = time.process_time()
        project()
        component_cpu_ms[name] = (time.process_time() - component_started) * 1000

    cpu_started = time.process_time()
    wall_started = time.perf_counter()
    responses = [
        client.get("/api/plugins/kanban/stats/observability?days=7")
        for _ in range(5)
    ]
    wall_ms = (time.perf_counter() - wall_started) * 1000 / len(responses)
    cpu_ms = (time.process_time() - cpu_started) * 1000 / len(responses)

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["usage"]["state"] == "fresh" for response in responses)
    print(
        f"live-size observability benchmark: rows={target_rows:,}; "
        f"mean_cpu={cpu_ms:.1f}ms; mean_wall={wall_ms:.1f}ms; "
        f"components_cpu_ms={component_cpu_ms}"
    )
    assert cpu_ms < 200, f"{target_rows=:,}; mean warm CPU={cpu_ms:.1f} ms"
    assert wall_ms < 300, f"{target_rows=:,}; mean warm wall={wall_ms:.1f} ms"


def test_usage_projection_cache_is_identity_scoped_and_single_flight(monkeypatch):
    module = _load_plugin_module()
    module.clear_usage_projection_cache()
    calls: list[tuple[str, int, str]] = []
    started = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(
        module,
        "_usage_cache_identity",
        lambda *, board, days: (board, days, "data-v1"),
    )

    def build(*, board, days, generated_at):
        calls.append((board, days, generated_at))
        started.set()
        assert release.wait(timeout=2)
        return {
            "available": True,
            "state": "fresh",
            "reason": None,
            "captured_at": generated_at,
            "summary": {"fact_rows": 94_682},
        }

    monkeypatch.setattr(module, "_usage_projection", build)
    results: list[dict] = []
    leader = threading.Thread(
        target=lambda: results.append(
            module._cached_usage_projection(
                board="default", days=7, generated_at="first"
            )
        )
    )
    waiter = threading.Thread(
        target=lambda: results.append(
            module._cached_usage_projection(
                board="default", days=7, generated_at="second"
            )
        )
    )
    leader.start()
    assert started.wait(timeout=1)
    waiter.start()
    time.sleep(0.05)
    assert len(calls) == 1
    release.set()
    leader.join(timeout=2)
    waiter.join(timeout=2)

    assert len(calls) == 1
    assert len(results) == 2
    assert all(result["state"] == "fresh" for result in results)
    assert all(result["cache"]["age_seconds"] == 0 for result in results)

    # A different board cannot reuse the default board's projection.
    release.set()
    other = module._cached_usage_projection(
        board="other", days=7, generated_at="third"
    )
    assert other["summary"]["fact_rows"] == 94_682
    assert len(calls) == 2


def test_profiles_identity_ignores_sqlite_shm_read_churn(tmp_path):
    module = _load_plugin_module()
    profile = tmp_path / "profiles" / "coder"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (profile / "state.db").write_bytes(b"db")
    wal = profile / "state.db-wal"
    wal.write_bytes(b"wal")
    shm = profile / "state.db-shm"
    shm.write_bytes(b"shm")

    before = module._profiles_identity(tmp_path / "profiles")
    shm.write_bytes(b"read-side shared-memory churn")
    after_shm_read = module._profiles_identity(tmp_path / "profiles")
    wal.write_bytes(b"durable wal revision")
    after_wal_write = module._profiles_identity(tmp_path / "profiles")

    assert after_shm_read == before
    assert after_wal_write != after_shm_read


def test_usage_projection_refresh_failure_returns_explicit_stale(monkeypatch):
    module = _load_plugin_module()
    module.clear_usage_projection_cache()
    identity = ["data-v1"]
    monkeypatch.setattr(
        module,
        "_usage_cache_identity",
        lambda *, board, days: (board, days, identity[0]),
    )
    monkeypatch.setattr(
        module,
        "_usage_projection",
        lambda **kwargs: {
            "available": True,
            "state": "fresh",
            "reason": None,
            "captured_at": kwargs["generated_at"],
            "summary": {"fact_rows": 94_682},
        },
    )
    fresh = module._cached_usage_projection(
        board="default", days=7, generated_at="first"
    )
    assert fresh["state"] == "fresh"

    identity[0] = "data-v2"

    def fail(**_kwargs):
        raise RuntimeError("read failed")

    monkeypatch.setattr(module, "_usage_projection", fail)
    stale = module._cached_usage_projection(
        board="default", days=7, generated_at="second"
    )

    assert stale["available"] is True
    assert stale["state"] == "stale"
    assert stale["reason"] == "refresh_failed:RuntimeError"
    assert stale["summary"]["fact_rows"] == 94_682
    assert stale["cache"]["age_seconds"] >= 0


def test_group_tokens_does_not_coerce_missing_value_to_zero():
    module = _load_plugin_module()

    assert module._group_tokens({"tokens": {"context_input": {"tokens": 0}}}, "context_input") == 0
    assert module._group_tokens({"tokens": {"context_input": {"tokens": None}}}, "context_input") is None
    assert module._group_tokens({}, "context_input") is None


def test_cost_coverage_can_be_reaggregated_without_losing_unknown_facts():
    module = _load_plugin_module()
    groups = [
        {
            "billing": {
                "metered": {
                    "metered_usd": {
                        "known_amount_usd": "0.000000",
                        "priced_breakdowns": 1,
                        "unpriced_breakdowns": 0,
                        "lower_bound_breakdowns": 0,
                    }
                }
            }
        },
        {
            "billing": {
                "metered": {
                    "metered_usd": {
                        "known_amount_usd": None,
                        "priced_breakdowns": 0,
                        "unpriced_breakdowns": 1,
                        "lower_bound_breakdowns": 0,
                    }
                }
            }
        },
    ]

    assert module._group_cost_coverage(groups) == {
        "known_amount_usd": "0.000000",
        "observed_facts": 1,
        "denominator_facts": 2,
        "lower_bound_facts": 0,
        "status": "partial",
    }


def test_observability_route_is_read_only(observability_app):
    client, kanban_path, usage_path = observability_app
    kanban_observer = sqlite3.connect(kanban_path)
    usage_observer = sqlite3.connect(usage_path)
    try:
        before = (
            kanban_observer.execute("PRAGMA data_version").fetchone()[0],
            usage_observer.execute("PRAGMA data_version").fetchone()[0],
        )
        response = client.get("/api/plugins/kanban/stats/observability")
        after = (
            kanban_observer.execute("PRAGMA data_version").fetchone()[0],
            usage_observer.execute("PRAGMA data_version").fetchone()[0],
        )
    finally:
        kanban_observer.close()
        usage_observer.close()

    assert response.status_code == 200
    assert after == before


def test_run_duration_projection_fails_soft_on_legacy_board(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "legacy.db"
    sqlite3.connect(path).close()
    module = _load_plugin_module()
    monkeypatch.setattr(
        module,
        "_conn",
        lambda _board: sqlite3.connect(path),
    )

    assert module._run_duration_projection(board="default", days=7) == {
        "p50_seconds": None,
        "p95_seconds": None,
        "count": 0,
        "source": "kanban.metric_scores.run_duration_seconds",
    }
