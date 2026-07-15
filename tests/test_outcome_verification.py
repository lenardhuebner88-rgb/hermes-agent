from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import outcome_verification as outcomes


@pytest.fixture()
def outcome_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_terminal_no_delivery_is_not_applicable() -> None:
    normalized = outcomes.normalize_outcome_fields(
        {
            "finding_state": "stale",
            "decision_state": "dismissed",
            "delivery_state": "none",
        },
        source="autoresearch",
    )

    assert normalized == {
        "outcome_applicability": "not_applicable",
        "measurement_status": "not_started",
        "outcome_verdict": None,
        "evidence_grade": "legacy_observational",
        "calibration_eligible": False,
    }
    assert outcomes.outcome_enforcement_enabled() is False


def test_pending_contract_cannot_claim_verified_evidence_grade() -> None:
    normalized = outcomes.normalize_outcome_fields(
        {
            "delivery_state": "queued",
            "measurement_status": "pending",
            "outcome_verdict": None,
            "contract_hash": "a" * 64,
            "evidence_grade": "contract_verified",
        },
        source="autoresearch",
    )
    assert normalized["evidence_grade"] == "legacy_observational"


def test_legacy_measured_strategist_verdict_is_preserved() -> None:
    normalized = outcomes.normalize_outcome_fields(
        {"status": "measured", "verdict": "improved"},
        source="strategist",
    )

    assert normalized["outcome_applicability"] == "applicable"
    assert normalized["measurement_status"] == "measured"
    assert normalized["outcome_verdict"] == "improved"
    assert normalized["evidence_grade"] == "legacy_observational"
    assert normalized["calibration_eligible"] is True


def test_probe_contract_is_canonical_and_rejects_arbitrary_paths(tmp_path: Path) -> None:
    proposal = {
        "id": "p1",
        "mode": "code",
        "target": "hermes_cli/example.py",
        "category": "silent_except",
        "finding_fingerprint": "fingerprint",
    }
    first = outcomes.build_probe_contract(proposal, repo_root=tmp_path)
    second = outcomes.build_probe_contract(dict(reversed(list(proposal.items()))), repo_root=tmp_path)

    assert first == second
    assert first["probe_id"] == "source_pattern.v1"
    assert first["contract_id"].startswith("outcome:")
    assert first["outcome_contract_version"] == 1
    assert first["contract_sha256"] == first["contract_hash"]
    assert len(first["contract_sha256"]) == 64
    assert first["claim"]
    assert first["measurement_kind"] == "metric_delta"
    assert first["success_template_id"]
    assert first["success_parameters"]
    assert first["success_rule"]
    assert first["outcome_class"].endswith("/v1")
    assert first["sampling_plan"]["sample_count"] == 1
    assert first["observation_window"]["kind"] == "immediate"
    assert first["trigger"] == "integrated_commit"
    assert first["measurement_budget"]["max_attempts"] == 3
    assert first["measurement_budget"]["max_output_bytes"] > 0
    assert first["measurement_budget"]["max_memory_mb"] > 0
    assert first["measurement_budget"]["max_cost_usd"] == 0.0
    assert first["calibration_eligible"] is False

    with pytest.raises(outcomes.ContractError, match="repository-relative"):
        outcomes.build_probe_contract({**proposal, "target": "/etc/passwd"}, repo_root=tmp_path)
    with pytest.raises(outcomes.ContractError, match="traversal"):
        outcomes.build_probe_contract({**proposal, "target": "../secret"}, repo_root=tmp_path)


def test_contract_registration_and_event_are_idempotent(outcome_home: Path) -> None:
    target = outcome_home.parent / "hermes_cli" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("try:\n    work()\nexcept Exception:\n    pass\n", encoding="utf-8")
    contract = outcomes.build_probe_contract(
        {
            "id": "proposal-1",
            "mode": "code",
            "target": "hermes_cli/example.py",
            "category": "silent_except",
        },
        repo_root=outcome_home.parent,
    )
    baseline = outcomes.capture_probe(contract, repo_root=outcome_home.parent)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="blocked outcome task",
            created_by="autoresearch",
            assignee="coder",
            kind="code",
            initial_status="blocked",
            idempotency_key="autoresearch:proposal-1",
        )
        first = outcomes.register_contract(
            conn,
            proposal_id="proposal-1",
            task_id=task_id,
            contract=contract,
            baseline=baseline,
            release_fingerprint="release-fingerprint",
        )
        second = outcomes.register_contract(
            conn,
            proposal_id="proposal-1",
            task_id=task_id,
            contract=contract,
            baseline=baseline,
            release_fingerprint="release-fingerprint",
        )
        events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, outcomes.CONTRACT_EVENT),
        ).fetchall()
        rows = conn.execute("SELECT * FROM outcome_contracts").fetchall()
        projected = outcomes.enrich_autoresearch_outcomes(
            [
                {
                    "id": "proposal-1",
                    "finding_state": "verified",
                    "decision_state": "accepted",
                    "delivery_state": "queued",
                }
            ],
            conn=conn,
        )[0]

    assert first is True
    assert second is False
    assert len(rows) == 1
    assert len(events) == 1
    event_payload = json.loads(events[0]["payload"])
    assert event_payload["contract_hash"] == contract["contract_hash"]
    assert event_payload["contract"] == contract
    assert event_payload["baseline"]["evidence_ref"] == baseline["evidence_ref"]
    assert projected["measurement_status"] == "pending"
    assert projected["evidence_grade"] == "legacy_observational"


def test_contract_registration_rejects_forged_hash(outcome_home: Path) -> None:
    contract = outcomes.build_probe_contract(
        {
            "id": "proposal-forged",
            "mode": "code",
            "target": "hermes_cli/example.py",
            "category": "silent_except",
        },
        repo_root=outcome_home.parent,
    )
    contract["contract_hash"] = "not-a-sha"
    contract["contract_sha256"] = "not-a-sha"
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="forged", created_by="autoresearch")
        with pytest.raises(outcomes.ContractError, match="hash"):
            outcomes.register_contract(
                conn,
                proposal_id="proposal-forged",
                task_id=task_id,
                contract=contract,
                baseline={"ok": True, "value": 1},
                release_fingerprint="forged",
            )


def test_contract_registration_rejects_tampered_baseline_evidence(
    outcome_home: Path,
) -> None:
    target = outcome_home.parent / "hermes_cli" / "tampered.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("try:\n    work()\nexcept Exception:\n    pass\n", encoding="utf-8")
    contract = outcomes.build_probe_contract(
        {"id": "tampered", "target": "hermes_cli/tampered.py", "category": "silent_except"},
        repo_root=outcome_home.parent,
    )
    baseline = outcomes.capture_probe(contract, repo_root=outcome_home.parent)
    baseline["value"] = 999
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="tampered baseline", created_by="autoresearch")
        with pytest.raises(outcomes.ContractError, match="seal"):
            outcomes.register_contract(
                conn,
                proposal_id="tampered",
                task_id=task_id,
                contract=contract,
                baseline=baseline,
                release_fingerprint="tampered",
            )


def test_vision_baseline_requires_fresh_typed_source_metadata() -> None:
    contract = outcomes.build_vision_metric_contract("autonomy_pct", direction=1)
    now = time.time()
    baseline = outcomes.capture_vision_snapshot_baseline(
        contract,
        {
            "schema_version": 3,
            "generated_at": now - 60,
            "metrics": {"autonomy_pct": 75.0},
        },
        now=now,
    )
    assert baseline["source_schema_version"] == "3"
    assert baseline["source_generated_at"] == pytest.approx(now - 60)
    assert baseline["value"] == pytest.approx(75.0)
    with pytest.raises(outcomes.ContractError, match="schema"):
        outcomes.capture_vision_snapshot_baseline(
            contract,
            {"generated_at": now, "metrics": {"autonomy_pct": 75.0}},
            now=now,
        )
    with pytest.raises(outcomes.ContractError, match="stale"):
        outcomes.capture_vision_snapshot_baseline(
            contract,
            {
                "schema_version": 3,
                "generated_at": now - 90_000,
                "metrics": {"autonomy_pct": 75.0},
            },
            now=now,
        )


def test_common_vision_contract_preserves_five_percent_neutral_band() -> None:
    contract = outcomes.build_vision_metric_contract("autonomy_pct", direction=1)
    assert contract["success_rule"]["neutral_tolerance"] == pytest.approx(0.05)
    baseline = {
        "ok": True,
        "environment_fingerprint": "same",
        "source_schema_version": "3",
        "observed_value": {"ok": True, "value": 100.0},
        "counter_observations": [],
    }
    neutral = {
        **baseline,
        "observed_value": {"ok": True, "value": 104.9},
    }
    improved = {
        **baseline,
        "observed_value": {"ok": True, "value": 105.0},
    }
    assert outcomes.compare_observations(contract, baseline, neutral) == "neutral"
    assert outcomes.compare_observations(contract, baseline, improved) == "improved"


def _claim_once(db_path: str, task_id: str, queue: multiprocessing.Queue) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        claim = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id="proposal-claim",
            contract_hash="a" * 64,
            phase="forward",
            attempt_no=1,
            lease_seconds=60,
        )
        queue.put(bool(claim))
    finally:
        conn.close()


def _run_verifier_once(db_path: str, repo_root: str, queue: multiprocessing.Queue) -> None:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        queue.put(
            outcomes.run_shadow_verifier(
                conn=conn,
                repo_root=Path(repo_root),
                phase="forward",
                require_enabled=False,
            )
        )
    finally:
        conn.close()


def test_two_processes_get_exactly_one_measurement_owner(outcome_home: Path) -> None:
    with kb.connect() as conn:
        outcomes.ensure_schema(conn)
        task_id = kb.create_task(conn, title="claim", created_by="test")
        db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [ctx.Process(target=_claim_once, args=(db_path, task_id, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    assert sorted(queue.get(timeout=5) for _ in processes) == [False, True]
    with kb.connect() as conn:
        starts = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, outcomes.MEASUREMENT_STARTED_EVENT),
        ).fetchone()[0]
        attempts = conn.execute("SELECT COUNT(*) FROM outcome_attempts").fetchone()[0]
    assert starts == attempts == 1


def test_two_verifier_processes_emit_one_attempt_event_and_cost(
    outcome_home: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "runner-repo"
    test_file = repo / "tests" / "test_claim.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_claim():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Outcome Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "outcome@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "tests/test_claim.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    contract = outcomes.build_probe_contract(
        {
            "id": "p-runner-race",
            "mode": "test",
            "target": "tests/test_claim.py",
            "affected_tests": ["tests/test_claim.py"],
        },
        repo_root=repo,
    )
    baseline = outcomes.capture_probe(contract, repo_root=repo)
    assert baseline["value"] == 1
    test_file.write_text(
        "import time\n\ndef test_claim():\n    time.sleep(0.4)\n    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "tests/test_claim.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fix"], check=True)
    integrated_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="runner race", created_by="autoresearch")
        outcomes.register_contract(
            conn,
            proposal_id="p-runner-race",
            task_id=task_id,
            contract=contract,
            baseline=baseline,
            release_fingerprint="race",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, "integration_merged", {"merge_commit": integrated_sha}
            )
            kb._append_event(
                conn, task_id, "INTEGRATOR_VERIFIED", {"merge_commit": integrated_sha}
            )
        db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_run_verifier_once, args=(db_path, str(repo), queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    summaries = [queue.get(timeout=5) for _ in processes]
    assert sum(summary["measured"] for summary in summaries) == 1
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(cost_usd) AS cost FROM outcome_attempts"
        ).fetchone()
        starts = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
            (task_id, outcomes.MEASUREMENT_STARTED_EVENT),
        ).fetchone()[0]
        completes = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
            (task_id, outcomes.MEASUREMENT_COMPLETED_EVENT),
        ).fetchone()[0]
    assert row["n"] == starts == completes == 1
    assert row["cost"] == pytest.approx(0.0)


def test_finalize_attempt_charges_cost_and_emits_event_once(outcome_home: Path) -> None:
    with kb.connect() as conn:
        outcomes.ensure_schema(conn)
        task_id = kb.create_task(conn, title="finalize", created_by="test")
        claim = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id="proposal-finalize",
            contract_hash="a" * 64,
            phase="forward",
            attempt_no=1,
        )
        assert claim
        assert outcomes.finalize_measurement_attempt(
            conn,
            dedupe_key=claim.dedupe_key,
            owner_token=claim.owner_token,
            status="measured",
            verdict="improved",
            observation={"value": 0, "evidence_ref": "outcome-evidence:sha256:test"},
            cost_breakdown={"research_usd": 0.1, "delivery_usd": 0.15},
            source_refs=["task-run:1"],
        )
        assert not outcomes.finalize_measurement_attempt(
            conn,
            dedupe_key=claim.dedupe_key,
            owner_token=claim.owner_token,
            status="measured",
            verdict="improved",
            observation={"value": 0},
            cost_usd=0.25,
        )
        row = conn.execute(
            "SELECT cost_usd, cost_breakdown_json, source_refs_json, verdict "
            "FROM outcome_attempts"
        ).fetchone()
        event_rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, outcomes.MEASUREMENT_COMPLETED_EVENT),
        ).fetchall()

    assert row["cost_usd"] == pytest.approx(0.25)
    assert json.loads(row["cost_breakdown_json"]) == {
        "delivery_usd": 0.15,
        "research_usd": 0.1,
    }
    assert json.loads(row["source_refs_json"]) == ["task-run:1"]
    assert row["verdict"] == "improved"
    assert len(event_rows) == 1
    assert json.loads(event_rows[0]["payload"])["cost_breakdown"]["delivery_usd"] == 0.15


def test_measurement_accounting_includes_every_component_once(outcome_home: Path) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="accounting", created_by="autoresearch")
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, cost_usd) "
            "VALUES (?, 'coder', 'done', 1, 0.20)",
            (task_id,),
        )
        delivery_run = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, cost_usd) "
            "VALUES (?, 'reviewer', 'done', 2, 0.30)",
            (task_id,),
        )
        review_run = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "operator_decision", {"decision": "release"})
        breakdown, refs, interventions = outcomes._measurement_accounting(
            conn,
            task_id=task_id,
            baseline={
                "research_cost_usd": 0.10,
                "cost_usd": 0.01,
                "evidence_ref": "outcome-evidence:sha256:baseline",
            },
            observation={
                "cost_usd": 0.02,
                "evidence_ref": "outcome-evidence:sha256:observation",
            },
        )
    assert breakdown == {
        "research_usd": 0.10,
        "delivery_usd": 0.20,
        "review_usd": 0.30,
        "baseline_probe_usd": 0.01,
        "outcome_probe_usd": 0.02,
    }
    assert f"task-run:{delivery_run}" in refs
    assert f"task-run:{review_run}" in refs
    assert interventions == 1


def test_code_probe_uses_real_integrator_merge_commit(outcome_home: Path) -> None:
    contract = outcomes.build_probe_contract(
        {"id": "p-sha", "mode": "test", "target": "tests/test_example.py", "affected_tests": ["tests/test_example.py"]},
        repo_root=outcome_home.parent,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="delivery", created_by="autoresearch")
        readiness = outcomes.measurement_readiness(conn, task_id=task_id, contract=contract)
        assert readiness == {"ready": False, "reason": "integration_sha_missing", "integration_sha": None}
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "integration_merged", {"commit_sha": "f" * 40})
            kb._append_event(conn, task_id, "INTEGRATOR_VERIFIED", {"commit_sha": "f" * 40})
        assert outcomes.measurement_readiness(
            conn, task_id=task_id, contract=contract
        )["ready"] is False
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "integration_merged", {"merge_commit": "a" * 40})
            kb._append_event(conn, task_id, "INTEGRATOR_VERIFIED", {"merge_commit": "a" * 40})
        readiness = outcomes.measurement_readiness(conn, task_id=task_id, contract=contract)

    assert readiness == {"ready": True, "reason": None, "integration_sha": "a" * 40}


def test_runtime_probe_requires_deployment_and_running_sha(outcome_home: Path) -> None:
    contract = outcomes.build_probe_contract(
        {
            "id": "p-runtime",
            "mode": "runtime",
            "measurement_kind": "runtime_observation",
            "metric_key": "autonomy_pct",
            "target": "hermes_cli/vision_metrics.py",
        },
        repo_root=outcome_home.parent,
    )
    assert contract["trigger"] == "deployed_runtime"
    assert contract["observation_window"]["min_age_seconds"] == 3 * 86_400
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="runtime", created_by="autoresearch")
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "integration_merged", {"merge_commit": "a" * 40})
        assert outcomes.measurement_readiness(conn, task_id=task_id, contract=contract)["ready"] is False
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "deployment_verified",
                {"deployed_sha": "a" * 40, "running_sha": "a" * 40},
            )
            conn.execute(
                "UPDATE task_events SET created_at=1000 WHERE task_id=? AND kind=?",
                (task_id, "deployment_verified"),
            )
        immature = outcomes.measurement_readiness(
            conn, task_id=task_id, contract=contract, now=1_001
        )
        readiness = outcomes.measurement_readiness(
            conn,
            task_id=task_id,
            contract=contract,
            now=1_000 + 3 * 86_400,
        )
    assert immature["reason"] == "observation_window_not_mature"
    assert readiness == {"ready": True, "reason": None, "integration_sha": "a" * 40}


def test_runtime_probe_marks_stale_source_snapshot_confounded(
    outcome_home: Path,
) -> None:
    contract = outcomes.build_vision_metric_contract("autonomy_pct", direction=1)
    metrics = outcome_home / "state" / "vision-metrics.json"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "generated_at": time.time() - 90_000,
                "metrics": {"autonomy_pct": 80.0},
            }
        ),
        encoding="utf-8",
    )
    evidence = outcomes.capture_probe(contract, repo_root=outcome_home.parent)
    assert "stale_source_snapshot" in evidence["confounded_reasons"]

    metrics.write_text(
        json.dumps({"metrics": {"autonomy_pct": 80.0}}),
        encoding="utf-8",
    )
    missing_metadata = outcomes.capture_probe(contract, repo_root=outcome_home.parent)
    assert "source_timestamp_invalid" in missing_metadata["confounded_reasons"]
    assert missing_metadata["source_schema_version"] is None


def test_overlapping_same_class_runtime_windows_are_confounded(outcome_home: Path) -> None:
    contract = outcomes.build_vision_metric_contract("autonomy_pct", direction=1)
    now = time.time()
    baseline = outcomes.capture_vision_snapshot_baseline(
        contract,
        {
            "schema_version": 3,
            "generated_at": now,
            "metrics": {"autonomy_pct": 70.0},
        },
        now=now,
    )
    with kb.connect() as conn:
        first = kb.create_task(conn, title="first runtime", created_by="strategist")
        second = kb.create_task(conn, title="second runtime", created_by="strategist")
        outcomes.register_contract(
            conn,
            proposal_id="runtime-first",
            task_id=first,
            contract=contract,
            baseline=baseline,
            release_fingerprint="first",
            source="strategist",
        )
        outcomes.register_contract(
            conn,
            proposal_id="runtime-second",
            task_id=second,
            contract=contract,
            baseline=baseline,
            release_fingerprint="second",
            source="strategist",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                first,
                "deployment_verified",
                {"deployed_sha": "a" * 40, "running_sha": "a" * 40},
            )
            kb._append_event(
                conn,
                second,
                "deployment_verified",
                {"deployed_sha": "b" * 40, "running_sha": "b" * 40},
            )
            conn.execute(
                "UPDATE task_events SET created_at=1000 WHERE task_id=? AND kind=?",
                (first, "deployment_verified"),
            )
            conn.execute(
                "UPDATE task_events SET created_at=2000 WHERE task_id=? AND kind=?",
                (second, "deployment_verified"),
            )
        readiness = outcomes.measurement_readiness(
            conn,
            task_id=first,
            contract=contract,
            now=1_000 + 3 * 86_400,
        )
    assert readiness["ready"] is True
    assert readiness["confounded_reasons"] == ["overlapping_effect_window"]
    assert readiness["confounded_task_ids"] == [second]


def test_shadow_verifier_measures_real_source_change_once(outcome_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    target = repo / "hermes_cli" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("try:\n    work()\nexcept Exception:\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Outcome Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "outcome@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "hermes_cli/example.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    proposal = {
        "id": "p-shadow",
        "mode": "code",
        "target": "hermes_cli/example.py",
        "category": "silent_except",
    }
    contract = outcomes.build_probe_contract(proposal, repo_root=repo)
    baseline = outcomes.capture_probe(contract, repo_root=repo)
    assert baseline["value"] == 1

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="shadow", created_by="autoresearch")
        outcomes.register_contract(
            conn,
            proposal_id="p-shadow",
            task_id=task_id,
            contract=contract,
            baseline=baseline,
            release_fingerprint="shadow-release",
        )
        target.write_text("try:\n    work()\nexcept Exception as exc:\n    raise RuntimeError() from exc\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "hermes_cli/example.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fix"], check=True)
        integrated_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, "integration_merged", {"merge_commit": integrated_sha}
            )
            kb._append_event(
                conn, task_id, "INTEGRATOR_VERIFIED", {"merge_commit": integrated_sha}
            )
        first = outcomes.run_shadow_verifier(
            conn=conn,
            repo_root=repo,
            require_enabled=False,
            phase="canary",
        )
        second = outcomes.run_shadow_verifier(
            conn=conn,
            repo_root=repo,
            require_enabled=False,
            phase="canary",
        )
        row = conn.execute("SELECT status, verdict, integration_sha FROM outcome_attempts").fetchone()

    assert first["measured"] == 1
    assert first["outcomes"][0]["verdict"] == "improved"
    assert second["measured"] == 0
    assert second["skipped_existing"] == 1
    assert dict(row) == {
        "status": "measured",
        "verdict": "improved",
        "integration_sha": integrated_sha,
    }


def test_runner_does_not_start_attempt_two_while_attempt_one_is_measuring(
    outcome_home: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    target = repo / "hermes_cli" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("try:\n    work()\nexcept Exception:\n    pass\n", encoding="utf-8")
    contract = outcomes.build_probe_contract(
        {"id": "p-active", "target": "hermes_cli/example.py", "category": "silent_except"},
        repo_root=repo,
    )
    baseline = outcomes.capture_probe(contract, repo_root=repo)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="active", created_by="autoresearch")
        outcomes.register_contract(
            conn,
            proposal_id="p-active",
            task_id=task_id,
            contract=contract,
            baseline=baseline,
            release_fingerprint="active",
        )
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "integration_merged", {"merge_commit": "c" * 40})
        claim = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id="p-active",
            contract_hash=contract["contract_hash"],
            phase="shadow",
            attempt_no=1,
            lease_seconds=300,
        )
        assert claim is not None
        summary = outcomes.run_shadow_verifier(
            conn=conn, repo_root=repo, require_enabled=False, phase="shadow"
        )
        attempts = conn.execute(
            "SELECT attempt_no, status FROM outcome_attempts ORDER BY attempt_no"
        ).fetchall()
        starts = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
            (task_id, outcomes.MEASUREMENT_STARTED_EVENT),
        ).fetchone()[0]
    assert summary["measured"] == 0
    assert summary["pending"] == 1
    assert [tuple(row) for row in attempts] == [(1, "measuring")]
    assert starts == 1


def test_expired_final_lease_is_persisted_as_exhausted(outcome_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "expired-repo"
    target = repo / "hermes_cli" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("try:\n    work()\nexcept Exception:\n    pass\n", encoding="utf-8")
    contract = outcomes.build_probe_contract(
        {"id": "p-expired", "target": "hermes_cli/example.py", "category": "silent_except"},
        repo_root=repo,
    )
    baseline = outcomes.capture_probe(contract, repo_root=repo)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="expired", created_by="autoresearch")
        outcomes.register_contract(
            conn,
            proposal_id="p-expired",
            task_id=task_id,
            contract=contract,
            baseline=baseline,
            release_fingerprint="expired",
        )
        claim = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id="p-expired",
            contract_hash=contract["contract_hash"],
            phase="shadow",
            attempt_no=3,
        )
        assert claim is not None
        conn.execute(
            "UPDATE outcome_attempts SET lease_expires_at=1 WHERE dedupe_key=?",
            (claim.dedupe_key,),
        )
        conn.commit()
        assert outcomes.recover_expired_attempts(conn, now=2) == 1
        attempt = conn.execute(
            "SELECT status, verdict, observation_json FROM outcome_attempts"
        ).fetchone()
        completion = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?",
            (task_id, outcomes.MEASUREMENT_COMPLETED_EVENT),
        ).fetchone()
    assert attempt["status"] == "exhausted"
    assert attempt["verdict"] == "unmeasurable"
    assert json.loads(attempt["observation_json"])["error"] == "lease_expired"
    assert json.loads(completion["payload"])["status"] == "exhausted"


def test_counter_violation_wins_and_environment_drift_is_confounded() -> None:
    contract = {
        "success_rule": {"metric": "returncode", "operator": "failing_to_passing"},
        "counter_rules": [
            {"metric": "returncode", "operator": "must_remain_passing"}
        ],
    }
    baseline = {
        "ok": True,
        "environment_fingerprint": "stable",
        "observed_value": {"metric": "returncode", "value": 1, "ok": True},
        "counter_observations": [{"metric": "returncode", "value": 0, "ok": True}],
    }
    counter_failed = {
        "ok": True,
        "environment_fingerprint": "stable",
        "observed_value": {"metric": "returncode", "value": 0, "ok": True},
        "counter_observations": [{"metric": "returncode", "value": 1, "ok": True}],
    }
    drifted = {**counter_failed, "environment_fingerprint": "changed"}
    assert outcomes.compare_observations(contract, baseline, counter_failed) == "worsened"
    assert outcomes.compare_observations(contract, baseline, drifted) == "confounded"


def test_environment_drift_is_observed_not_rejected_as_an_invalid_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "hermes_cli" / "environment.py"
    target.parent.mkdir(parents=True)
    target.write_text("try:\n    work()\nexcept Exception:\n    pass\n", encoding="utf-8")
    contract = outcomes.build_probe_contract(
        {"id": "environment", "target": "hermes_cli/environment.py", "category": "silent_except"},
        repo_root=tmp_path,
    )
    baseline = outcomes.capture_probe(contract, repo_root=tmp_path)
    original_descriptor = outcomes._environment_descriptor

    def _drifted_descriptor(value):
        return {**original_descriptor(value), "platform_machine": "drifted-machine"}

    monkeypatch.setattr(outcomes, "_environment_descriptor", _drifted_descriptor)
    # The sealed pre-mutation baseline remains valid evidence even though the
    # current executor changed. The new observation records that drift and the
    # comparator makes it non-directional.
    outcomes.validate_baseline(contract, baseline)
    current = outcomes.capture_probe(contract, repo_root=tmp_path)
    assert outcomes.compare_observations(contract, baseline, current) == "confounded"


def test_source_pattern_counter_violation_wins_over_primary_improvement(
    tmp_path: Path,
) -> None:
    target = tmp_path / "hermes_cli" / "counter_canary.py"
    test_file = tmp_path / "tests" / "test_counter_canary.py"
    target.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    target.write_text("def value():\n    return 0\n", encoding="utf-8")
    test_file.write_text(
        "from hermes_cli.counter_canary import value\n\n"
        "def test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    contract = outcomes.build_probe_contract(
        {
            "id": "counter-contract",
            "mode": "test",
            "target": "hermes_cli/counter_canary.py",
            "affected_tests": ["tests/test_counter_canary.py"],
            "counter_patterns": [
                {
                    "path": "hermes_cli/counter_canary.py",
                    "pattern_rule": "silent_except",
                }
            ],
        },
        repo_root=tmp_path,
    )
    baseline = outcomes.capture_probe(contract, repo_root=tmp_path)
    assert baseline["value"] == 1
    assert baseline["counter_observations"][0]["value"] == 0
    target.write_text(
        "def value():\n    return 1\n\n"
        "COUNTER = '''\ntry:\n    work()\nexcept Exception:\n    pass\n'''\n",
        encoding="utf-8",
    )
    current = outcomes.capture_probe(contract, repo_root=tmp_path)
    assert current["value"] == 0
    assert current["counter_observations"][0]["value"] == 1
    assert outcomes.compare_observations(contract, baseline, current) == "worsened"


def test_verified_metrics_exclude_legacy_improved_and_publish_exact_denominators() -> None:
    items = [
        {
            "id": "legacy",
            "delivery_state": "integrated",
            "outcome_applicability": "applicable",
            "measurement_status": "measured",
            "outcome_verdict": "improved",
            "evidence_grade": "legacy_observational",
            "outcome_cost_usd": 2.0,
        },
        {
            "id": "verified-improved",
            "delivery_state": "integrated",
            "outcome_applicability": "applicable",
            "measurement_status": "measured",
            "outcome_verdict": "improved",
            "evidence_grade": "contract_verified",
            "outcome_cost_usd": 3.0,
            "outcome_operator_interventions": 1,
        },
        {
            "id": "verified-neutral",
            "delivery_state": "integrated",
            "outcome_applicability": "applicable",
            "measurement_status": "measured",
            "outcome_verdict": "neutral",
            "evidence_grade": "contract_verified",
            "outcome_cost_usd": 1.0,
        },
        {
            "id": "pending",
            "delivery_state": "integrated",
            "outcome_applicability": "applicable",
            "measurement_status": "pending",
            "outcome_verdict": None,
            "evidence_grade": "contract_verified",
        },
    ]
    metrics = outcomes.outcome_metrics(items)
    assert metrics["verified_improved"] == 1
    assert metrics["legacy_improved"] == 1
    assert metrics["verified_directional_denominator"] == 2
    assert metrics["verified_benefit_rate"] == pytest.approx(0.5)
    assert metrics["outcome_coverage"] == pytest.approx(0.5)
    assert metrics["directional_coverage"] == pytest.approx(0.5)
    assert metrics["cost_per_verified_benefit_usd"] == pytest.approx(6.0)
    assert metrics["operator_interventions_per_verified_benefit"] == pytest.approx(1.0)


def test_strategist_projection_uses_task_events_and_preserves_measured_legacy(
    outcome_home: Path,
) -> None:
    with kb.connect() as conn:
        archived = kb.create_task(conn, title="archived", created_by="strategist")
        integrated = kb.create_task(conn, title="integrated", created_by="strategist")
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (archived,))
        with kb.write_txn(conn):
            kb._append_event(
                conn, integrated, "integration_merged", {"merge_commit": "d" * 40}
            )
            kb._append_event(
                conn, integrated, "INTEGRATOR_VERIFIED", {"merge_commit": "d" * 40}
            )
        records = [
            {"root_task_id": archived, "status": "proposed", "verdict": None},
            {"root_task_id": integrated, "status": "shipped", "verdict": None},
            {
                "root_task_id": archived,
                "status": "measured",
                "verdict": "improved",
                "measured_at": 123,
            },
        ]
        projected = outcomes.project_strategist_outcomes(
            records, conn=conn, terminalize_missing=True
        )

    assert projected[0]["status"] == "archived"
    assert projected[0]["outcome_applicability"] == "not_applicable"
    assert projected[0]["measurement_status"] == "not_started"
    assert projected[0]["outcome_verdict"] is None
    assert projected[1]["status"] == "shipped"
    assert projected[1]["outcome_delivery_sha"] == "d" * 40
    assert projected[1]["measurement_status"] == "pending"
    assert projected[2]["verdict"] == "improved"
    assert projected[2]["measured_at"] == 123
    assert projected[2]["evidence_grade"] == "legacy_observational"


def test_shared_state_migration_dry_run_backup_and_idempotence(tmp_path: Path) -> None:
    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    proposal_path = proposals_dir / "p1.json"
    proposal_path.write_text(
        json.dumps(
            {
                "id": "p1",
                "status": "routed_to_kanban",
                "finding_state": "verified",
                "decision_state": "accepted",
                "delivery_state": "integrated",
            }
        ),
        encoding="utf-8",
    )
    strategist_path = tmp_path / "state" / "strategist" / "lever-outcomes.json"
    strategist_path.parent.mkdir(parents=True)
    strategist_path.write_text(
        json.dumps(
            [
                {
                    "schema_version": 1,
                    "root_task_id": "t1",
                    "status": "measured",
                    "verdict": "improved",
                    "measured_at": 123,
                }
            ]
        ),
        encoding="utf-8",
    )
    before_proposal = proposal_path.read_bytes()
    before_strategist = strategist_path.read_bytes()
    db_path = tmp_path / "kanban.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")

    dry = outcomes.migrate_shared_state(
        proposals_dir=proposals_dir,
        strategist_outcomes_path=strategist_path,
        kanban_db_path=db_path,
        apply=False,
    )
    assert dry["proposal_changes"] == 1
    assert dry["strategist_changes"] == 1
    assert proposal_path.read_bytes() == before_proposal
    assert strategist_path.read_bytes() == before_strategist
    assert dry["schema_changes"] == 6
    with sqlite3.connect(db_path) as conn:
        assert not outcomes._table_exists(conn, "outcome_contracts")

    applied = outcomes.migrate_shared_state(
        proposals_dir=proposals_dir,
        strategist_outcomes_path=strategist_path,
        kanban_db_path=db_path,
        apply=True,
        backup_root=tmp_path / "backups",
    )
    assert applied["proposal_changes"] == 1
    assert applied["strategist_changes"] == 1
    assert applied["schema_changes"] == 6
    assert Path(applied["backup_dir"]).is_dir()
    assert (Path(applied["backup_dir"]) / "kanban.db").is_file()
    with sqlite3.connect(Path(applied["backup_dir"]) / "kanban.db") as backup_conn:
        assert not outcomes._table_exists(backup_conn, "outcome_contracts")
    with sqlite3.connect(db_path) as conn:
        assert outcomes._table_exists(conn, "outcome_contracts")
        assert outcomes._table_exists(conn, "outcome_attempts")
    migrated_proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    migrated_strategist = json.loads(strategist_path.read_text(encoding="utf-8"))[0]
    assert migrated_proposal["measurement_status"] == "exhausted"
    assert migrated_proposal["outcome_verdict"] == "unmeasurable"
    assert migrated_proposal["evidence_grade"] == "legacy_observational"
    assert migrated_strategist["verdict"] == "improved"
    assert migrated_strategist["measured_at"] == 123
    assert migrated_strategist["evidence_grade"] == "legacy_observational"

    again = outcomes.migrate_shared_state(
        proposals_dir=proposals_dir,
        strategist_outcomes_path=strategist_path,
        kanban_db_path=db_path,
        apply=True,
        backup_root=tmp_path / "backups",
    )
    assert again["proposal_changes"] == 0
    assert again["strategist_changes"] == 0
    assert again["schema_changes"] == 0
    assert again["backup_dir"] is None
