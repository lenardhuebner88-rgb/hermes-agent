from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sqlite3

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
        "measurement_status": "exhausted",
        "outcome_verdict": None,
        "evidence_grade": "contract_verified",
        "calibration_eligible": False,
    }


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
    assert len(first["contract_hash"]) == 64
    assert first["budget"] == {"max_attempts": 3, "max_samples": 1, "timeout_seconds": 30}

    with pytest.raises(outcomes.ContractError, match="repository-relative"):
        outcomes.build_probe_contract({**proposal, "target": "/etc/passwd"}, repo_root=tmp_path)
    with pytest.raises(outcomes.ContractError, match="traversal"):
        outcomes.build_probe_contract({**proposal, "target": "../secret"}, repo_root=tmp_path)


def test_contract_registration_and_event_are_idempotent(outcome_home: Path) -> None:
    contract = outcomes.build_probe_contract(
        {
            "id": "proposal-1",
            "mode": "code",
            "target": "hermes_cli/example.py",
            "category": "silent_except",
        },
        repo_root=outcome_home.parent,
    )
    baseline = {"value": 2, "sample_count": 1}
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

    assert first is True
    assert second is False
    assert len(rows) == 1
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["contract_hash"] == contract["contract_hash"]


def _claim_once(db_path: str, task_id: str, queue: multiprocessing.Queue) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        claim = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id="proposal-claim",
            contract_hash="hash",
            phase="forward",
            attempt_no=1,
            lease_seconds=60,
        )
        queue.put(bool(claim))
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


def test_finalize_attempt_charges_cost_and_emits_event_once(outcome_home: Path) -> None:
    with kb.connect() as conn:
        outcomes.ensure_schema(conn)
        task_id = kb.create_task(conn, title="finalize", created_by="test")
        claim = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id="proposal-finalize",
            contract_hash="hash",
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
            observation={"value": 0},
            cost_usd=0.25,
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
        row = conn.execute("SELECT cost_usd, verdict FROM outcome_attempts").fetchone()
        events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, outcomes.MEASUREMENT_COMPLETED_EVENT),
        ).fetchone()[0]

    assert row["cost_usd"] == pytest.approx(0.25)
    assert row["verdict"] == "improved"
    assert events == 1


def test_runtime_probe_waits_for_real_integration_sha(outcome_home: Path) -> None:
    contract = outcomes.build_probe_contract(
        {"id": "p-sha", "mode": "test", "target": "tests/test_example.py", "affected_tests": ["tests/test_example.py"]},
        repo_root=outcome_home.parent,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="delivery", created_by="autoresearch")
        readiness = outcomes.measurement_readiness(conn, task_id=task_id, contract=contract)
        assert readiness == {"ready": False, "reason": "integration_sha_missing", "integration_sha": None}
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "integration_merged", {"commit_sha": "a" * 40})
        readiness = outcomes.measurement_readiness(conn, task_id=task_id, contract=contract)

    assert readiness == {"ready": True, "reason": None, "integration_sha": "a" * 40}


def test_shadow_verifier_measures_real_source_change_once(outcome_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    target = repo / "hermes_cli" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("try:\n    work()\nexcept Exception:\n    pass\n", encoding="utf-8")
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
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "integration_merged", {"commit_sha": "b" * 40})
        target.write_text("try:\n    work()\nexcept Exception as exc:\n    raise RuntimeError() from exc\n", encoding="utf-8")
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
    assert dict(row) == {"status": "measured", "verdict": "improved", "integration_sha": "b" * 40}


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
    assert dry["schema_changes"] == 5
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
    assert applied["schema_changes"] == 5
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
