from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent.usage_pricing import CanonicalUsage, CostResult
from hermes_cli import active_provider_facts
from hermes_cli import usage_facts_readmodel as readmodel
from hermes_cli.usage_facts_db import upsert_run_facts

FIXTURE_ROOT = (
    Path(__file__).parents[1] / "fixtures" / "usage_facts_readmodel"
)
ORIGIN_FIXTURE = json.loads(
    (FIXTURE_ROOT / "origin_rows.json").read_text(encoding="utf-8")
)


def _normalize_fixture_row(row: dict[str, Any]) -> dict[str, Any]:
    observed = {
        column: int(row[column] is not None)
        for column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        )
    }
    return readmodel.normalize_token_totals(
        row["origin"],
        token_rows=1,
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        input_observed_rows=observed["input_tokens"],
        output_observed_rows=observed["output_tokens"],
        cache_read_observed_rows=observed["cache_read_tokens"],
        cache_write_observed_rows=observed["cache_write_tokens"],
        reasoning_observed_rows=observed["reasoning_tokens"],
    )


@pytest.mark.parametrize(
    "row",
    ORIGIN_FIXTURE["rows"],
    ids=lambda row: row["origin"],
)
def test_live_redacted_origin_rows_normalize_by_source_contract(
    row: dict[str, Any],
) -> None:
    normalized = _normalize_fixture_row(row)
    expected = row["expected"]

    assert normalized["input_semantics"] == expected["input_semantics"]
    assert (
        normalized["context_input"]["tokens"]
        == expected["context_input_tokens"]
    )
    assert (
        normalized["context_input"]["status"]
        == expected["context_input_status"]
    )
    assert normalized["new_input"]["tokens"] == expected["new_input_tokens"]
    assert normalized["new_input"]["status"] == expected["new_input_status"]
    assert (
        normalized["uncached_input"]["tokens"]
        == expected["uncached_input_tokens"]
    )
    assert (
        normalized["uncached_input"]["status"]
        == expected["uncached_input_status"]
    )


def test_cross_origin_sum_uses_context_input_not_native_input_column() -> None:
    rows = ORIGIN_FIXTURE["rows"]
    raw_input = sum(int(row["input_tokens"] or 0) for row in rows)
    context_input = sum(
        int(_normalize_fixture_row(row)["context_input"]["tokens"] or 0)
        for row in rows
    )
    claude = next(row for row in rows if row["origin"] == "claude_code")
    claude_context = _normalize_fixture_row(claude)["context_input"]["tokens"]

    assert context_input > raw_input
    assert claude_context == 104_682
    assert claude_context / claude["input_tokens"] > 17_000


def test_unknown_origin_is_not_guessed() -> None:
    normalized = readmodel.normalize_token_totals(
        "future_origin",
        token_rows=1,
        input_tokens=100,
        output_tokens=5,
        cache_read_tokens=90,
        cache_write_tokens=0,
        reasoning_tokens=None,
        input_observed_rows=1,
        output_observed_rows=1,
        cache_read_observed_rows=1,
        cache_write_observed_rows=1,
        reasoning_observed_rows=0,
    )

    assert normalized["input_semantics"] == readmodel.INPUT_NOT_DERIVABLE
    assert normalized["context_input"] == {
        "tokens": None,
        "status": readmodel.STATUS_UNAVAILABLE,
    }


def test_payload_separates_metered_quota_unattributed_and_kanban(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_path = tmp_path / "usage_facts.db"
    board_path = tmp_path / "kanban.db"
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    _seed_usage_fixture(usage_path)
    _seed_board_fixture(board_path)

    calls: list[str] = []

    def fake_cost(
        model_name: str,
        usage: CanonicalUsage,
        *,
        provider: str | None = None,
        **_kwargs: Any,
    ) -> CostResult:
        calls.append(f"metered:{provider}:{model_name}")
        amount = (
            Decimal(usage.input_tokens)
            + Decimal(usage.output_tokens) * 2
            + Decimal(usage.cache_read_tokens) / 10
            + Decimal(usage.cache_write_tokens) * Decimal("1.25")
        ) / Decimal(1_000_000)
        amount += Decimal(usage.request_count) / 100
        return CostResult(
            amount_usd=amount,
            status="estimated",
            source="user_override",
            label="fixture",
            pricing_version="fixture-v1",
        )

    def fake_equivalent(
        model_name: str,
        usage: CanonicalUsage,
        *,
        provider: str | None = None,
        **kwargs: Any,
    ) -> CostResult:
        calls.append(f"equivalent:{provider}:{model_name}")
        result = fake_cost(
            model_name,
            usage,
            provider=provider,
            **kwargs,
        )
        return CostResult(
            amount_usd=result.amount_usd,
            status="equivalent",
            source=result.source,
            label="fixture equivalent",
            pricing_version=result.pricing_version,
        )

    monkeypatch.setattr(readmodel, "estimate_usage_cost", fake_cost)
    monkeypatch.setattr(
        readmodel,
        "estimate_equivalent_cost",
        fake_equivalent,
    )

    payload = readmodel.build_usage_facts_payload(
        usage_path,
        kanban_path=board_path,
        profiles_root=profiles_root,
        generated_at="2026-07-27T15:36:00+00:00",
    )

    assert payload["contract_version"] == readmodel.CONTRACT_VERSION
    assert payload["summary"]["raw_input_tokens"] < (
        payload["summary"]["tokens"]["context_input_tokens"]
    )
    assert payload["summary"]["billing"]["quota"]["marginal_usd"] == "0"
    assert (
        payload["summary"]["billing"]["metered"]["metered_usd"][
            "known_amount_usd"
        ]
        != "0.000000"
    )
    assert (
        payload["summary"]["billing"]["api_equivalent_usd"][
            "known_amount_usd"
        ]
        != "0.000000"
    )
    assert (
        payload["summary"]["billing"]["unclassified"]["api_equivalent_usd"][
            "status"
        ]
        in {"complete", "partial", "unknown"}
    )
    unclassified_equivalent = payload["summary"]["billing"]["unclassified"][
        "api_equivalent_usd"
    ]
    if unclassified_equivalent["status"] == "unknown":
        assert unclassified_equivalent["known_amount_usd"] is None
    assert payload["unattributed"]["label"] == "nicht_zuordenbar"
    assert payload["unattributed"]["fact_rows"] == 1
    assert payload["unattributed"]["by_origin"][0]["origin"] == "grok_cli"

    kanban = payload["kanban"]
    assert kanban["available"] is True
    assert kanban["total_runs"] == 4
    assert sum(kanban["provider_classification"].values()) == 4
    assert (
        kanban["provider_classification"][
            active_provider_facts.CLASS_UNKNOWN
        ]
        == 1
    )
    assert kanban["usage_coverage"] == {
        "token_bearing_runs": 1,
        "provider_only_fact_runs": 0,
        "runs_without_fact": 3,
        "state": "thin",
    }
    assert calls


def test_aggregate_normalized_tokens_preserves_unknown_and_observed_zero():
    complete_zero = {
        "observed_rows": {
            "token_rows": 1,
            "input": 1,
            "output": 1,
            "cache_read": 1,
            "cache_write": 1,
        },
        "raw_input_tokens": 0,
        "context_input": {"tokens": 0, "status": readmodel.STATUS_EXACT},
        "new_input": {"tokens": 0, "status": readmodel.STATUS_EXACT},
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": None,
        "semantics": readmodel.INPUT_CACHE_INCLUSIVE,
    }
    missing = {
        "observed_rows": {
            "token_rows": 1,
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
        },
        "raw_input_tokens": None,
        "context_input": {"tokens": None, "status": readmodel.STATUS_UNAVAILABLE},
        "new_input": {"tokens": None, "status": readmodel.STATUS_UNAVAILABLE},
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "semantics": readmodel.INPUT_NOT_DERIVABLE,
    }

    aggregate = readmodel.aggregate_normalized_tokens([complete_zero, missing])

    assert aggregate["output_tokens"] == 0
    assert aggregate["coverage"]["output_tokens"] == {
        "observed_rows": 1,
        "denominator_rows": 2,
        "status": "partial",
    }
    unknown = readmodel.aggregate_normalized_tokens([missing])
    assert unknown["output_tokens"] is None
    assert unknown["coverage"]["output_tokens"]["status"] == "unknown"


def test_payload_calibrates_route_changes_from_board_events(
    tmp_path: Path,
) -> None:
    usage_path = tmp_path / "usage_facts.db"
    board_path = tmp_path / "kanban.db"
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    _seed_usage_fixture(usage_path)
    _seed_board_fixture(board_path)

    calibration = readmodel.build_usage_facts_payload(
        usage_path,
        kanban_path=board_path,
        profiles_root=profiles_root,
    )["kanban"]["route_change_calibration"]

    assert calibration == {
        "source": "task_events.kind=model_route_changed",
        "scope": "all events with complete old and new provider/model routes",
        "observed_events": 3,
        "route_changed_events": 2,
        "route_unchanged_events": 1,
        "route_change_rate": pytest.approx(2 / 3),
    }


def test_payload_omits_route_change_calibration_without_board_database(
    tmp_path: Path,
) -> None:
    usage_path = tmp_path / "usage_facts.db"
    _seed_usage_fixture(usage_path)

    kanban = readmodel.build_usage_facts_payload(usage_path)["kanban"]

    assert kanban == {
        "available": False,
        "reason": "kanban_database_unavailable",
    }
    assert "route_change_calibration" not in kanban


def test_payload_uses_latest_rate_limit_snapshot_per_origin_and_marks_stale(
    tmp_path: Path,
) -> None:
    usage_path = tmp_path / "usage_facts.db"
    sidecar_path = tmp_path / "foreign_rate_limit_snapshots.jsonl"
    _seed_usage_fixture(usage_path)
    sidecar_path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "captured_at": "2026-07-27T14:00:00+00:00",
                    "origin": "codex_cli",
                    "run_id": "old-run",
                    "context_window": 42,
                    "rate_limits": {"primary": {"used_percent": 10}},
                },
                {
                    "captured_at": "2026-07-27T15:59:00+00:00",
                    "origin": "codex_cli",
                    "run_id": "new-run",
                    "context_window": 84,
                    "rate_limits": {"primary": {"used_percent": 25}},
                },
                {
                    "captured_at": "2026-07-26T15:00:00+00:00",
                    "origin": "kimi_cli",
                    "run_id": "stale-run",
                    "context_window": 21,
                    "rate_limits": {"primary": {"used_percent": 90}},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    payload = readmodel.build_usage_facts_payload(
        usage_path,
        rate_limit_path=sidecar_path,
        generated_at="2026-07-27T16:00:00+00:00",
    )

    snapshots = payload["rate_limits"]
    assert snapshots["available"] is True
    assert snapshots["snapshots"] == {
        "codex_cli": {
            "captured_at": "2026-07-27T15:59:00+00:00",
            "age_seconds": 60,
            "freshness": "fresh",
            "rate_limits": {"primary": {"used_percent": 25}},
        },
        "kimi_cli": {
            "captured_at": "2026-07-26T15:00:00+00:00",
            "age_seconds": 90_000,
            "freshness": "stale",
            "rate_limits": {"primary": {"used_percent": 90}},
        },
    }
    assert "grok_cli" not in snapshots["snapshots"]
    assert "run_id" not in json.dumps(snapshots)
    assert "context_window" not in json.dumps(snapshots)


def test_payload_keeps_usage_facts_usable_when_rate_limit_sidecar_is_missing(
    tmp_path: Path,
) -> None:
    usage_path = tmp_path / "usage_facts.db"
    _seed_usage_fixture(usage_path)

    payload = readmodel.build_usage_facts_payload(
        usage_path,
        rate_limit_path=tmp_path / "missing.jsonl",
    )

    assert payload["summary"]["fact_rows"] > 0
    assert payload["rate_limits"] == {
        "available": False,
        "reason": "sidecar_unavailable",
        "snapshots": {},
    }


def test_s7_example_fixture_matches_contract_version() -> None:
    example = json.loads(
        (FIXTURE_ROOT / "s7_payload_example.json").read_text(encoding="utf-8")
    )

    assert example["contract_version"] == readmodel.CONTRACT_VERSION
    assert example["normalization"]["version"] == (
        readmodel.NORMALIZATION_VERSION
    )
    assert example["groups"][0]["key"] == {
        "origin": "claude_code",
        "profile": None,
        "lane": "redacted-claude-session",
        "model": "qwen3.8-max-preview",
        "model_label": "qwen3.8-max-preview",
    }
    assert {
        readmodel.BILLING_METERED,
        readmodel.BILLING_QUOTA,
        readmodel.BILLING_UNCLASSIFIED,
    }.issubset(example["summary"]["billing"])


def test_attributed_payload_requires_exact_run_for_tasks_and_chains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_path = tmp_path / "usage_facts.db"

    def fake_equivalent(
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

    monkeypatch.setattr(
        readmodel,
        "estimate_equivalent_cost",
        fake_equivalent,
    )
    for run_id, fields in (
        (
            "exact-run",
            {
                "task_run_id": "101",
                "task_id": "task-exact",
                "chain_id": "chain-exact",
                "board": "main",
            },
        ),
        (
            "task-only",
            {
                "task_id": "task-history-only",
                "chain_id": "chain-history-only",
                "board": "main",
            },
        ),
        ("unattributed", {}),
    ):
        upsert_run_facts(
            run_id,
            {
                "origin": "hermes_agent",
                "provider": "fixture-provider",
                "model": "fixture-model",
                "billing_mode": "subscription_included",
                "input_tokens": 100,
                "cache_read_tokens": 10,
                "cache_write_tokens": 0,
                "output_tokens": 5,
                "captured_at": "2026-07-30T12:00:00+00:00",
                "source": "measured",
                **fields,
            },
            path=usage_path,
        )
    upsert_run_facts(
        "exact-run-second-model-call",
        {
            "origin": "claude_code",
            "task_run_id": "101",
            "task_id": "task-exact",
            "chain_id": "chain-exact",
            "board": "main",
            "provider": "fixture-provider",
            "model": "fixture-second-model",
            "billing_mode": "subscription_included",
            "input_tokens": 50,
            "cache_read_tokens": 5,
            "cache_write_tokens": 0,
            "output_tokens": 2,
            "captured_at": "2026-07-30T12:00:00+00:00",
            "source": "measured",
        },
        path=usage_path,
    )

    payload = readmodel.build_attributed_usage_payload(
        usage_path,
        generated_at="2026-07-30T13:00:00+00:00",
    )

    assert payload["coverage"]["fact_rows"] == 4
    assert payload["fact_coverage"]["denominator_kind"] == "usage_fact_rows"
    assert payload["fact_coverage"]["exact_task_run"] == payload["coverage"][
        "exact_task_run"
    ]
    assert payload["execution_adoption"] == {
        "observed_executions": None,
        "denominator_executions": None,
        "ratio": None,
        "status": "unknown",
        "reason": "universal_execution_denominator_unavailable",
    }
    assert payload["coverage"]["exact_task_run"]["observed_rows"] == 2
    assert payload["coverage"]["task_only_rows"] == 1
    assert payload["unknown"]["task_only_rows_not_promoted"] == 1
    assert payload["summary"]["fact_rows"] == 4
    assert payload["summary"]["billing"]["api_equivalent_usd"]["status"] == (
        "complete"
    )
    assert payload["tasks"]["total_buckets"] == 1
    assert payload["tasks"]["buckets"][0]["key"]["task_id"] == "task-exact"
    assert payload["tasks"]["buckets"][0]["run_count"] == 1
    assert payload["chains"]["total_buckets"] == 1
    assert payload["chains"]["buckets"][0]["key"]["chain_id"] == "chain-exact"
    assert payload["chains"]["buckets"][0]["run_count"] == 1
    assert payload["models"]["total_buckets"] == 2
    assert (
        payload["tasks"]["buckets"][0]["billing"]["api_equivalent_usd"][
            "status"
        ]
        == "complete"
    )
    assert payload["tasks"]["buckets"][0]["freshness"] == {
        "latest_captured_at": "2026-07-30T12:00:00+00:00",
        "age_seconds": 3600,
        "status": "fresh",
    }


def test_price_serialization_preserves_sub_microcent_precision() -> None:
    # Run 8802's Kimi K3 API-equivalent is 0.2116692 USD.  A derived value
    # must remain an exact decimal string until an explicitly chosen display
    # formatter rounds it.
    assert readmodel._decimal_string(Decimal("0.2116692")) == "0.2116692"


def test_workload_split_uses_discovered_subagent_profiles_and_keeps_unknown(
    tmp_path: Path,
) -> None:
    """Real Claude transcript forms have both canonical and legacy call kinds."""

    usage_path = tmp_path / "usage_facts.db"
    rows = (
        ("main", None, 10),
        ("subagent", "general-purpose", 20),
        ("general-purpose", None, 30),
        ("subagent", "Explore", 40),
        ("Explore", None, 50),
        ("foreign_cli", None, 60),
        (None, None, 70),
    )
    for index, (call_kind, profile, input_tokens) in enumerate(rows, start=1):
        upsert_run_facts(
            f"call-kind-{index}",
            {
                "origin": "claude_code",
                "call_kind": call_kind,
                "profile": profile,
                "input_tokens": input_tokens,
                "cache_read_tokens": input_tokens * 9,
                "cache_write_tokens": 0,
                "output_tokens": 1,
                "captured_at": "2026-07-27T15:36:00+00:00",
                "source": "measured",
            },
            path=usage_path,
        )

    workload = readmodel.build_usage_facts_payload(usage_path)["summary"][
        "workload"
    ]

    assert workload["main"] == {
        "fact_rows": 1,
        "context_input_tokens": 100,
        "coverage": {"observed_rows": 1, "denominator_rows": 1, "status": "complete"},
    }
    assert workload["subagent"] == {
        "fact_rows": 4,
        "context_input_tokens": 1_400,
        "coverage": {"observed_rows": 4, "denominator_rows": 4, "status": "complete"},
    }
    assert workload["unknown"] == {
        "fact_rows": 2,
        "context_input_tokens": 1_300,
        "coverage": {"observed_rows": 2, "denominator_rows": 2, "status": "complete"},
    }
    assert workload["subagent_share"] == {
        "context_input_tokens": 1_400,
        "all_context_input_tokens": 2_800,
        "of_all_context": 0.5,
        "classified_context_input_tokens": 1_500,
        "of_classified_context": pytest.approx(14 / 15),
        "classification_status": "partial",
    }


def _seed_usage_fixture(path: Path) -> None:
    for index, row in enumerate(ORIGIN_FIXTURE["rows"], start=1):
        upsert_run_facts(
            f"fixture-{index}",
            {
                key: value
                for key, value in row.items()
                if key
                in {
                    "origin",
                    "profile",
                    "lane",
                    "model",
                    "provider",
                    "billing_mode",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                }
            }
            | {
                "captured_at": "2026-07-27T15:36:00+00:00",
                "source": "measured",
            },
            path=path,
        )

    upsert_run_facts(
        "fixture-metered-exact",
        {
            "origin": "qwen_cli",
            "provider": "fixture-provider",
            "model": "fixture-metered-model",
            "profile": "fixture-profile",
            "lane": "fixture-lane",
            "billing_mode": "api_key",
            "input_tokens": 1000,
            "cache_read_tokens": 100,
            "cache_write_tokens": 50,
            "output_tokens": 100,
            "reasoning_tokens": 0,
            "llm_call_count": 1,
            "captured_at": "2026-07-27T15:36:00+00:00",
            "source": "measured",
        },
        path=path,
    )
    upsert_run_facts(
        "2",
        {
            "origin": "hermes_agent",
            "provider": "fixture-stamped-provider",
            "model": "fixture-stamped-model",
            "billing_mode": "subscription_included",
            "input_tokens": 10,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "captured_at": "2026-07-27T15:36:00+00:00",
            "source": "measured",
        },
        path=path,
    )


def _seed_board_fixture(path: Path) -> None:
    rows = [
        ("1", "scheduled", None, "worker", "{}"),
        (
            "2",
            "completed",
            "fixture-stamped-provider",
            "worker",
            '{"model":"fixture-stamped-model"}',
        ),
        ("3", "completed", None, "worker", "{}"),
        (
            "4",
            "completed",
            None,
            "worker",
            (
                '{"model":"fixture-reconstructed-model",'
                '"provider":"fixture-reconstructed-provider"}'
            ),
        ),
    ]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE task_runs (
                id TEXT PRIMARY KEY,
                status TEXT,
                active_provider TEXT,
                profile TEXT,
                metadata TEXT,
                started_at REAL,
                ended_at REAL,
                last_heartbeat_at REAL
            )
            """
        )
        connection.execute("CREATE TABLE lanes (profiles TEXT)")
        connection.execute("INSERT INTO lanes VALUES ('{}')")
        connection.execute(
            """
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                run_id INTEGER,
                kind TEXT NOT NULL,
                payload TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO task_runs (
                id, status, active_provider, profile, metadata
            ) VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.executemany(
            """
            INSERT INTO task_events (task_id, kind, payload, created_at)
            VALUES (?, 'model_route_changed', ?, ?)
            """,
            [
                (
                    "fixture-task",
                    json.dumps(
                        {
                            "old": {
                                "provider": "fixture-a",
                                "model": "fixture-model-a",
                            },
                            "new": {
                                "provider": "fixture-a",
                                "model": "fixture-model-a",
                            },
                        }
                    ),
                    1,
                ),
                (
                    "fixture-task",
                    json.dumps(
                        {
                            "old": {
                                "provider": "fixture-a",
                                "model": "fixture-model-a",
                            },
                            "new": {
                                "provider": "fixture-b",
                                "model": "fixture-model-b",
                            },
                        }
                    ),
                    2,
                ),
                (
                    "fixture-task",
                    json.dumps(
                        {
                            "old": {
                                "provider": "fixture-b",
                                "model": "fixture-model-b",
                            },
                            "new": {
                                "provider": "fixture-b",
                                "model": "fixture-model-c",
                            },
                        }
                    ),
                    3,
                ),
            ],
        )
