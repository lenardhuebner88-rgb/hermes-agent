from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


def _load_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "observability_sentinel_smoke.py"
    )
    spec = importlib.util.spec_from_file_location(
        "observability_sentinel_smoke_test_module",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_weekly_events_use_stable_identity_but_real_observation_time() -> None:
    module = _load_module()
    first = module.sentinel_events(
        datetime(2026, 7, 30, 1, tzinfo=timezone.utc)
    )
    second = module.sentinel_events(
        datetime(2026, 7, 31, 23, tzinfo=timezone.utc)
    )

    assert [event.idempotency_key for event in first] == [
        event.idempotency_key for event in second
    ]
    assert first[0].observed_at_ms != second[0].observed_at_ms
    assert first[0].observed_at_ms == 1_785_373_200_000
    assert [event.lifecycle_phase.value for event in first] == [
        "process_started",
        "ended",
    ]
    serialized = "".join(event.to_json() for event in first)
    assert "prompt" not in serialized
    assert "command" not in serialized
    assert "kanban" not in serialized


def test_probe_proves_append_dedupe_projection_outcome_and_unknown_cost(
    tmp_path: Path,
) -> None:
    module = _load_module()
    report = module.run_probe(
        tmp_path / "execution-facts.db",
        now=datetime(2026, 7, 30, 13, tzinfo=timezone.utc),
    )

    assert report["status"] == "pass"
    assert all(report["checks"].values())
    assert report["checks"]["operational_isolation"] is True
    assert report["checks"]["unknown_cost_not_zero"] is True


def test_stale_weekly_fact_cannot_degrade_alert_or_work_metrics(
    tmp_path: Path,
) -> None:
    module = _load_module()
    now = datetime(2026, 8, 2, 23, tzinfo=timezone.utc)

    report = module.run_probe(
        tmp_path / "execution-facts.db",
        now=now,
    )

    assert report["status"] == "pass"
    assert report["checks"]["outcome"] is True
    assert report["checks"]["operational_isolation"] is True


def test_weekly_rerun_reuses_first_exact_observation_for_dedupe(
    tmp_path: Path,
) -> None:
    module = _load_module()
    database = tmp_path / "execution-facts.db"
    first = module.run_probe(
        database,
        now=datetime(2026, 7, 30, 13, tzinfo=timezone.utc),
    )
    second = module.run_probe(
        database,
        now=datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
    )

    assert first["status"] == "pass"
    assert second["status"] == "pass"
    assert second["execution_id"] == first["execution_id"]


def test_status_receipt_preserves_last_success_on_failure() -> None:
    module = _load_module()
    checked = datetime(2026, 7, 30, 13, tzinfo=timezone.utc)
    receipt = module.status_receipt(
        {
            "contract_version": module.CONTRACT_VERSION,
            "status": "fail",
            "checks": {
                "append": True,
                "dedupe": False,
            },
        },
        checked_at=checked,
        previous={"last_success_at": "2026-07-23T13:00:00+00:00"},
    )

    assert receipt["status"] == "failed"
    assert receipt["last_success_at"] == "2026-07-23T13:00:00+00:00"
    assert receipt["checks"] == {"append": True, "dedupe": False}
    assert receipt["activation_effect"] == "execution_facts_only"


def test_main_writes_only_content_free_status_and_explicit_database(
    tmp_path: Path,
) -> None:
    module = _load_module()
    status_path = tmp_path / "status.json"
    database = tmp_path / "facts.db"

    exit_code = module.main(
        [
            "--db",
            str(database),
            "--status-path",
            str(status_path),
            "--lock-path",
            str(tmp_path / "sentinel.lock"),
        ]
    )

    assert exit_code == 0
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert database.is_file()
    serialized = json.dumps(payload)
    assert "prompt" not in serialized
    assert "response" not in serialized
