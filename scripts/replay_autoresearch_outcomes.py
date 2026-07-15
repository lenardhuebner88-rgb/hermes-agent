#!/usr/bin/env python3
"""Reproducible Autoresearch outcome replay and post-discovery canary harness.

This is an operator evidence tool, not a background service.  Every mode uses
isolated temporary state, local-only Git commits, real OS processes and the
selected checkout's actual modules.  It never contacts or pushes a remote.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence


SCRIPT = Path(__file__).resolve()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _configure(root: Path, state: Path, *, repo_root: Path | None = None) -> None:
    sys.path.insert(0, str(root))
    scripts = root / "scripts"
    if scripts.is_dir():
        sys.path.insert(0, str(scripts))
    home = state / "home"
    audit = state / "audit"
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_AUTORESEARCH_AUDIT_DIR"] = str(audit)
    os.environ["HERMES_AUTORESEARCH_DIGEST_PATH"] = str(state / "digest.json")
    os.environ["HERMES_AUTORESEARCH_RECONCILE_SUMMARY_PATH"] = str(state / "last-reconcile.json")
    os.environ["HERMES_AUTORESEARCH_RECONCILE_LOCK_PATH"] = str(state / "reconcile.lock")
    os.environ["HERMES_STRATEGIST_VETOED_PATH"] = str(state / "vetoed.json")
    os.environ["HERMES_KANBAN_DB"] = str(home / "kanban.db")
    os.environ["HERMES_KANBAN_BOARD"] = "default"
    os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = str(state / "workspaces")
    os.environ["HERMES_KANBAN_WORKER_ISOLATION"] = "worktree"
    os.environ["HERMES_SANDBOX_MODE"] = "1"
    os.environ["HOME"] = str(home)
    if repo_root is not None:
        os.environ["AUTORESEARCH_REPLAY_REPO_ROOT"] = str(repo_root)
    home.mkdir(parents=True, exist_ok=True)
    audit.mkdir(parents=True, exist_ok=True)


def _parse_worker(stdout: str, stderr: str, returncode: int) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    parsed: dict[str, Any]
    try:
        parsed = json.loads(lines[-1]) if lines else {}
    except ValueError:
        parsed = {"stdout": stdout[-1000:]}
    parsed["returncode"] = returncode
    if stderr.strip():
        parsed["stderr"] = stderr[-1000:]
    return parsed


def _run_pair(root: Path, state: Path, *, max_new: int) -> list[dict[str, Any]]:
    start_at = time.time() + 0.7
    command = [
        sys.executable,
        str(SCRIPT),
        "_flood-worker",
        "--root",
        str(root),
        "--state",
        str(state),
        "--start-at",
        str(start_at),
        "--max-new",
        str(max_new),
    ]
    workers = [
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=60)
        results.append(_parse_worker(stdout, stderr, worker.returncode))
    return results


def flood_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    _configure(root, state)
    from hermes_cli import autoresearch_reconcile as reconcile

    real_route = reconcile._route_to_kanban
    delayed = {"done": False}

    def _slow_route(*route_args, **route_kwargs):
        if not delayed["done"]:
            delayed["done"] = True
            time.sleep(0.35)
        return real_route(*route_args, **route_kwargs)

    reconcile._route_to_kanban = _slow_route
    while time.time() < float(args.start_at):
        time.sleep(0.005)
    summary = reconcile.reconcile_proposals(max_new_tasks=int(args.max_new))
    print(_json(summary), flush=True)
    return 0


def _prepare_flood(root: Path, state: Path, count: int) -> None:
    _configure(root, state)
    from hermes_cli import autoresearch_proposals as proposals
    from hermes_cli import kanban_db as kb

    kb.init_db()
    targets = (
        "hermes_cli/autoresearch_reconcile.py",
        "hermes_cli/autoresearch_proposals.py",
        "hermes_cli/autoresearch_runs.py",
        "hermes_cli/strategist.py",
        "hermes_cli/kanban_db.py",
        "scripts/autoresearch_nightly.py",
        "scripts/autoresearch_v2_nightly.py",
        "tests/test_autoresearch_reconcile.py",
    )
    for index in range(count):
        proposal_id = f"replay-flood-{index:02d}"
        target = targets[index % len(targets)]
        proposals.save_proposal(
            {
                "id": proposal_id,
                "schema": proposals.PROPOSAL_SCHEMA,
                "mode": "code",
                "proposal_type": "deep_audit",
                "finding_id": proposal_id,
                "target": target,
                "target_path": target,
                "title": f"Replay finding {index:02d}",
                "category": "bug_risk",
                "theme": f"replay-{index:02d}",
                "severity": "high",
                "evidence": f"grounded replay evidence {index:02d}",
                "fix_hint": "Apply the narrow replay fix and prove it with a focused test.",
                "status": "proposed",
                "created_at": f"2026-07-14T00:{index:02d}:00Z",
            }
        )


def _flood_snapshot(root: Path, state: Path) -> dict[str, Any]:
    _configure(root, state)
    from hermes_cli import autoresearch_proposals as proposals
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_by = 'autoresearch'"
        ).fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    statuses: dict[str, int] = {}
    for proposal in proposals.list_proposals():
        status = str(proposal.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {"tasks": int(tasks), "task_events": int(events), "proposal_statuses": statuses}


def flood_replay(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(tempfile.mkdtemp(prefix=f"autoresearch-flood-{root.name}-", dir="/tmp"))
    _prepare_flood(root, state, int(args.proposals))
    waves: list[dict[str, Any]] = []
    for number in range(1, int(args.waves) + 1):
        before = _flood_snapshot(root, state)
        workers = _run_pair(root, state, max_new=int(args.max_new))
        after = _flood_snapshot(root, state)
        waves.append({"wave": number, "before": before, "workers": workers, "after": after})
    result = {
        "mode": "flood-replay",
        "root": str(root),
        "state": str(state),
        "proposals": int(args.proposals),
        "max_new": int(args.max_new),
        "waves": waves,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _frontend_split(root: Path, cards: list[dict[str, Any]], state: Path, *, label: str) -> dict[str, Any]:
    tsx = SCRIPT.parents[1] / "node_modules" / ".bin" / "tsx"
    if not tsx.is_file():
        raise RuntimeError(f"tsx toolchain missing: {tsx}")
    input_path = state / f"{label}-cards.json"
    runner_path = state / f"{label}-split.ts"
    input_path.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    module_url = (root / "web" / "src" / "control" / "lib" / "autoresearch.ts").resolve().as_uri()
    runner_path.write_text(
        "import fs from 'node:fs';\n"
        f"import {{ splitAutoresearchProposals }} from {json.dumps(module_url)};\n"
        f"const cards = JSON.parse(fs.readFileSync({json.dumps(str(input_path))}, 'utf8'));\n"
        "const split = splitAutoresearchProposals(cards);\n"
        "const ids = (items: Array<{id: string}>) => items.map((item) => item.id);\n"
        "console.log(JSON.stringify({actionable: ids(split.actionable), delivery: ids(split.delivery), integrated: ids(split.integrated), history: ids(split.history)}));\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(tsx), str(runner_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"frontend split replay failed: {completed.stderr[-1000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def lifecycle_replay(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    source_proposals = Path(args.proposals_dir).resolve()
    source_db = Path(args.kanban_db).resolve()
    state = Path(tempfile.mkdtemp(prefix=f"autoresearch-lifecycle-{root.name}-", dir="/tmp"))
    _configure(root, state)
    audit_proposals = state / "audit" / "proposals"
    shutil.copytree(source_proposals, audit_proposals)
    _sqlite_backup(source_db, state / "home" / "kanban.db")

    from hermes_cli import autoresearch_proposals as proposals

    payload = proposals.proposals_payload()
    cards = payload.get("proposals") or []
    integrated = int(payload.get("integrated_count") or 0)
    delivery = int(payload.get("delivery_count") or 0)
    open_count = int(payload.get("open_count") or 0)
    count = int(payload.get("count") or len(cards))
    contract_fixture = [
        {
            "id": "explicit-none-applied",
            "target": "fixture",
            "section": None,
            "rationale_plain": "fixture",
            "diff_before_after": "",
            "mode": "code",
            "status": "applied",
            "operator_action_required": False,
            "finding_state": "rejected",
            "decision_state": "dismissed",
            "delivery_state": "none",
            "decision_owner": "kanban",
        },
        {
            "id": "legacy-applied",
            "target": "fixture",
            "section": None,
            "rationale_plain": "fixture",
            "diff_before_after": "",
            "mode": "code",
            "status": "applied",
            "operator_action_required": False,
        },
        {
            "id": "queued",
            "target": "fixture",
            "section": None,
            "rationale_plain": "fixture",
            "diff_before_after": "",
            "mode": "code",
            "status": "routed_to_kanban",
            "operator_action_required": False,
            "delivery_state": "queued",
        },
        {
            "id": "integrated",
            "target": "fixture",
            "section": None,
            "rationale_plain": "fixture",
            "diff_before_after": "",
            "mode": "code",
            "status": "routed_to_kanban",
            "operator_action_required": False,
            "delivery_state": "integrated",
        },
    ]
    result = {
        "mode": "lifecycle-replay",
        "root": str(root),
        "state": str(state),
        "count": count,
        "open_count": open_count,
        "delivery_count": delivery,
        "integrated_count": integrated,
        "history_count": count - open_count - delivery - integrated,
        "dismissed_count": int(payload.get("dismissed_count") or 0),
        "stale_count": int(payload.get("stale_count") or 0),
        "explicit_none_count": sum(1 for card in cards if card.get("delivery_state") == "none"),
        "live_frontend_split": _frontend_split(root, cards, state, label="live"),
        "contract_frontend_split": _frontend_split(root, contract_fixture, state, label="contract"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _historical_command(command: Sequence[str], *, cwd: Path, timeout: int = 900) -> dict[str, Any]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"historical replay failed ({completed.returncode}): {completed.stderr[-2000:]}"
        )
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("historical replay did not emit one JSON document") from exc


def _historical_sha(root: Path) -> str:
    result = _run(["git", "rev-parse", "HEAD"], cwd=root)
    sha = result["output_tail"].strip().lower()
    if result["returncode"] != 0 or len(sha) != 40:
        raise RuntimeError(f"historical checkout has no exact commit: {root}")
    return sha


def _flood_contract_violations(result: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    max_new = int(result.get("max_new") or 0)
    violations = 0
    waves: list[dict[str, Any]] = []
    for index, wave in enumerate(result.get("waves") or []):
        before = wave.get("before") or {}
        after = wave.get("after") or {}
        task_delta = int(after.get("tasks") or 0) - int(before.get("tasks") or 0)
        event_delta = int(after.get("task_events") or 0) - int(
            before.get("task_events") or 0
        )
        workers = list(wave.get("workers") or [])
        busy = [
            worker
            for worker in workers
            if bool(worker.get("busy")) or worker.get("status") == "busy"
        ]
        owners = [worker for worker in workers if worker not in busy]
        loser_mutations = sum(
            int(worker.get(key) or 0)
            for worker in busy
            for key in ("seen", "new_tasks", "routed_to_kanban")
        )
        wave_violations = max(0, task_delta - max_new)
        wave_violations += abs(len(owners) - 1)
        wave_violations += loser_mutations
        if index > 0:
            wave_violations += abs(task_delta) + abs(event_delta)
            if before.get("proposal_statuses") != after.get("proposal_statuses"):
                wave_violations += 1
        violations += wave_violations
        waves.append(
            {
                "wave": int(wave.get("wave") or index + 1),
                "task_delta": task_delta,
                "event_delta": event_delta,
                "owners": len(owners),
                "busy_workers": len(busy),
                "loser_mutations": loser_mutations,
                "violations": wave_violations,
                "before": before,
                "after": after,
            }
        )
    return violations, {"max_new": max_new, "waves": waves}


def _lifecycle_contract_violations(
    result: Mapping[str, Any],
) -> tuple[int, dict[str, Any]]:
    split = result.get("contract_frontend_split") or {}
    locations = {
        name: set(split.get(name) or [])
        for name in ("actionable", "delivery", "integrated", "history")
    }
    violations = 0
    for name in ("actionable", "delivery", "integrated"):
        violations += int("explicit-none-applied" in locations[name])
    violations += int("explicit-none-applied" not in locations["history"])
    violations += int("legacy-applied" not in locations["integrated"])
    details = {
        "fixture_split": {key: sorted(value) for key, value in locations.items()},
        "snapshot_matrix": {
            "total": int(result.get("count") or 0),
            "actionable": int(result.get("open_count") or 0),
            "delivery": int(result.get("delivery_count") or 0),
            "integrated": int(result.get("integrated_count") or 0),
            "history": int(result.get("history_count") or 0),
            "dismissed": int(result.get("dismissed_count") or 0),
            "stale": int(result.get("stale_count") or 0),
        },
        "violations": violations,
    }
    return violations, details


def _persist_historical_attempt(
    *,
    case_id: str,
    baseline_sha: str,
    target_sha: str,
    baseline_value: int,
    target_value: int,
    baseline_details: Mapping[str, Any],
    target_details: Mapping[str, Any],
) -> dict[str, Any]:
    engine_root = SCRIPT.parents[1]
    state = Path(tempfile.mkdtemp(prefix=f"autoresearch-{case_id}-engine-", dir="/tmp"))
    _configure(engine_root, state, repo_root=engine_root)
    from hermes_cli import kanban_db as kb
    from hermes_cli import outcome_verification as outcomes

    contract = outcomes.build_historical_replay_contract(case_id)
    baseline = outcomes.seal_historical_replay_observation(
        contract,
        target_sha=baseline_sha,
        value=baseline_value,
        details=baseline_details,
    )
    observation = outcomes.seal_historical_replay_observation(
        contract,
        target_sha=target_sha,
        value=target_value,
        details=target_details,
    )
    verdict = outcomes.compare_observations(contract, baseline, observation)
    if baseline_value == 0:
        verdict = "unmeasurable"
    kb.init_db()
    proposal_id = f"historical-replay:{case_id}"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"Historical outcome replay: {case_id}",
            created_by="autoresearch-replay",
        )
        fingerprint = outcomes.release_fingerprint(
            proposal_id=proposal_id,
            contract=contract,
            baseline=baseline,
            target_sha256=target_sha,
        )
        outcomes.register_contract(
            conn,
            proposal_id=proposal_id,
            task_id=task_id,
            contract=contract,
            baseline=baseline,
            release_fingerprint=fingerprint,
            source="autoresearch",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, "integration_merged", {"merge_commit": target_sha}
            )
            kb._append_event(
                conn, task_id, "INTEGRATOR_VERIFIED", {"merge_commit": target_sha}
            )
        claim = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id=proposal_id,
            contract_hash=contract["contract_hash"],
            phase="replay",
            attempt_no=1,
        )
        if claim is None:
            raise RuntimeError("historical replay attempt was not claimable")
        if not outcomes.finalize_measurement_attempt(
            conn,
            dedupe_key=claim.dedupe_key,
            owner_token=claim.owner_token,
            status="measured",
            verdict=verdict,
            observation=observation,
            cost_breakdown={"replay_process_usd": 0.0},
            source_refs=[baseline["evidence_ref"], observation["evidence_ref"]],
            integration_sha=target_sha,
        ):
            raise RuntimeError("historical replay attempt did not finalize")
        duplicate = outcomes.claim_measurement_attempt(
            conn,
            task_id=task_id,
            proposal_id=proposal_id,
            contract_hash=contract["contract_hash"],
            phase="replay",
            attempt_no=1,
        )
        attempt = conn.execute(
            "SELECT status, verdict, cost_usd, cost_breakdown_json, "
            "source_refs_json, integration_sha FROM outcome_attempts"
        ).fetchone()
        event_counts = {
            row["kind"]: int(row["n"])
            for row in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM task_events GROUP BY kind"
            ).fetchall()
        }
    return {
        "engine_state": str(state),
        "task_id": task_id,
        "contract": contract,
        "baseline": baseline,
        "observation": observation,
        "attempt": {
            "status": attempt["status"],
            "verdict": attempt["verdict"],
            "cost_usd": float(attempt["cost_usd"] or 0.0),
            "cost_breakdown": json.loads(attempt["cost_breakdown_json"] or "{}"),
            "source_refs": json.loads(attempt["source_refs_json"] or "[]"),
            "integration_sha": attempt["integration_sha"],
        },
        "event_counts": event_counts,
        "duplicate_attempt_claimed": duplicate is not None,
    }


def flood_backtest(args: argparse.Namespace) -> int:
    parent = Path(args.parent_root).resolve()
    target = Path(args.target_root).resolve()
    common = ["--proposals", str(args.proposals), "--max-new", str(args.max_new)]
    parent_result = _historical_command(
        [sys.executable, str(SCRIPT), "flood", "--root", str(parent), *common, "--waves", "1"],
        cwd=SCRIPT.parents[1],
    )
    target_result = _historical_command(
        [sys.executable, str(SCRIPT), "flood", "--root", str(target), *common, "--waves", "1"],
        cwd=SCRIPT.parents[1],
    )
    idempotent_result = _historical_command(
        [
            sys.executable,
            str(SCRIPT),
            "flood",
            "--root",
            str(target),
            "--proposals",
            str(min(int(args.proposals), int(args.max_new))),
            "--max-new",
            str(args.max_new),
            "--waves",
            "2",
        ],
        cwd=SCRIPT.parents[1],
    )
    parent_value, parent_details = _flood_contract_violations(parent_result)
    target_value, target_details = _flood_contract_violations(target_result)
    idempotent_value, idempotent_details = _flood_contract_violations(idempotent_result)
    target_value += idempotent_value
    target_details = {
        "budget_replay": target_details,
        "idempotence_replay": idempotent_details,
    }
    evidence = _persist_historical_attempt(
        case_id="reconcile_flood_limit",
        baseline_sha=_historical_sha(parent),
        target_sha=_historical_sha(target),
        baseline_value=parent_value,
        target_value=target_value,
        baseline_details=parent_details,
        target_details=target_details,
    )
    print(
        json.dumps(
            {
                "mode": "flood-outcome-backtest",
                "parent_root": str(parent),
                "target_root": str(target),
                "parent_state": parent_result["state"],
                "target_state": target_result["state"],
                "target_idempotence_state": idempotent_result["state"],
                **evidence,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def lifecycle_backtest(args: argparse.Namespace) -> int:
    parent = Path(args.parent_root).resolve()
    target = Path(args.target_root).resolve()
    common = [
        "--proposals-dir",
        str(Path(args.proposals_dir).resolve()),
        "--kanban-db",
        str(Path(args.kanban_db).resolve()),
    ]
    parent_result = _historical_command(
        [sys.executable, str(SCRIPT), "lifecycle", "--root", str(parent), *common],
        cwd=SCRIPT.parents[1],
    )
    target_result = _historical_command(
        [sys.executable, str(SCRIPT), "lifecycle", "--root", str(target), *common],
        cwd=SCRIPT.parents[1],
    )
    parent_value, parent_details = _lifecycle_contract_violations(parent_result)
    target_value, target_details = _lifecycle_contract_violations(target_result)
    evidence = _persist_historical_attempt(
        case_id="explicit_lifecycle_truth",
        baseline_sha=_historical_sha(parent),
        target_sha=_historical_sha(target),
        baseline_value=parent_value,
        target_value=target_value,
        baseline_details=parent_details,
        target_details=target_details,
    )
    print(
        json.dumps(
            {
                "mode": "lifecycle-outcome-backtest",
                "parent_root": str(parent),
                "target_root": str(target),
                "parent_state": parent_result["state"],
                "target_state": target_result["state"],
                **evidence,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def e2e_reconcile_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    repo = Path(args.repo).resolve()
    _configure(root, state, repo_root=repo)
    from hermes_cli import autoresearch_reconcile as reconcile

    reconcile.REPO_ROOT = repo
    summary = reconcile.reconcile_proposals(
        max_new_tasks=int(args.max_new),
        min_task_severity=str(args.min_severity),
    )
    print(_json(summary))
    return 0


def e2e_dispatch_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    repo = Path(args.repo).resolve()
    _configure(root, state, repo_root=repo)
    from hermes_cli import kanban_db as kb

    # Only authentication discovery uses the operator's normal HOME. Hermes
    # state, proposal state, the board and every workspace remain isolated by
    # their explicit HERMES_* paths. No credential is copied or printed.
    os.environ["HOME"] = str(Path(args.auth_home).resolve())
    with kb.connect() as conn:
        result = kb.dispatch_once(
            conn,
            max_spawn=1,
            max_in_progress=1,
            serialize_by_repo=True,
            max_concurrent_per_repo=1,
        )
    if len(result.spawned) != 1:
        raise RuntimeError(f"production dispatcher did not spawn exactly one worker: {result}")
    task_id = str(result.spawned[0][0])
    deadline = time.monotonic() + 1_200
    terminal = None
    while time.monotonic() < deadline:
        with kb.connect() as conn:
            terminal = kb.get_task(conn, task_id)
            if terminal is not None and terminal.status in {
                "done",
                "blocked",
                "failed",
                "archived",
            }:
                break
        time.sleep(1)
    # `hermes kanban complete` closes the task just before the Claude CLI emits
    # its final result JSON. Poll the existing deferred backfill for a bounded
    # interval so this E2E records the real subscription-equivalent burn rather
    # than racing that final log line. Missing evidence remains partial below.
    cost_deadline = time.monotonic() + 30
    cost_backfilled_runs = 0
    task = None
    runs = []
    review_skips = []
    while True:
        with kb.connect() as conn:
            cost_backfilled_runs += kb.backfill_run_costs(conn, limit=10)
            task = kb.get_task(conn, task_id)
            runs = conn.execute(
                "SELECT id, status, profile, cost_usd, cost_status, requested_provider, "
                "requested_model, metadata FROM task_runs WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
            review_skips = conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? "
                "AND kind='review_skipped_deterministic' ORDER BY id",
                (task_id,),
            ).fetchall()
        if runs and all(row["cost_usd"] is not None for row in runs):
            break
        if time.monotonic() >= cost_deadline:
            break
        time.sleep(0.5)
    if task is None or task.status != "done":
        log_path = state / "home" / "logs" / f"{task_id}.log"
        log_tail = ""
        if log_path.is_file():
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(
            f"production worker did not finish task {task_id}: "
            f"status={getattr(task, 'status', None)} log_tail={log_tail}"
        )
    run_evidence = []
    for row in runs:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        run_evidence.append(
            {
                "id": int(row["id"]),
                "status": row["status"],
                "profile": row["profile"],
                "cost_usd": (
                    float(row["cost_usd"]) if row["cost_usd"] is not None else None
                ),
                "cost_status": (
                    row["cost_status"]
                    or (
                        metadata.get("cost", {}).get("cost_status")
                        if isinstance(metadata.get("cost"), dict) else None
                    )
                    or ("actual" if row["cost_usd"] is not None else "unknown")
                ),
                "cost_usd_equivalent": (
                    float(metadata["cost_usd_equivalent"])
                    if isinstance(metadata.get("cost_usd_equivalent"), (int, float))
                    and not isinstance(metadata.get("cost_usd_equivalent"), bool)
                    else None
                ),
                "billing_mode": metadata.get("billing_mode"),
                "requested_provider": row["requested_provider"],
                "requested_model": row["requested_model"],
                "commit": metadata.get("commit") if isinstance(metadata, dict) else None,
            }
        )
    print(
        _json(
            {
                "spawned": [list(item) for item in result.spawned],
                "skipped_locked": bool(result.skipped_locked),
                "task_id": task_id,
                "task_status": task.status,
                "worker_runs": run_evidence,
                "review_skipped_deterministic": len(review_skips),
                "provider_calls": len(run_evidence),
                "cost_backfilled_runs": int(cost_backfilled_runs),
                "provider_cost_status": (
                    "complete"
                    if all(
                        item["cost_usd"] is not None
                        and (
                            item["billing_mode"] != "subscription_included"
                            or item["cost_usd_equivalent"] is not None
                        )
                        for item in run_evidence
                    )
                    else "partial"
                ),
                "known_provider_cost_usd": round(
                    sum(float(item["cost_usd"] or 0.0) for item in run_evidence), 8
                ),
                "provider_cost_usd": (
                    round(sum(float(item["cost_usd"] or 0.0) for item in run_evidence), 8)
                    if all(item["cost_usd"] is not None for item in run_evidence)
                    else None
                ),
                "provider_cost_usd_equivalent": (
                    round(
                        sum(
                            float(item["cost_usd_equivalent"] or 0.0)
                            for item in run_evidence
                        ),
                        8,
                    )
                    if all(
                        item["billing_mode"] != "subscription_included"
                        or item["cost_usd_equivalent"] is not None
                        for item in run_evidence
                    )
                    else None
                ),
                "provider_effective_cost_usd": (
                    round(
                        sum(float(item["cost_usd"] or 0.0) for item in run_evidence)
                        + sum(
                            float(item["cost_usd_equivalent"] or 0.0)
                            for item in run_evidence
                        ),
                        8,
                    )
                    if all(
                        item["cost_usd"] is not None
                        and (
                            item["billing_mode"] != "subscription_included"
                            or item["cost_usd_equivalent"] is not None
                        )
                        for item in run_evidence
                    )
                    else None
                ),
            }
        )
    )
    return 0


def e2e_verifier_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    repo = Path(args.repo).resolve()
    _configure(root, state, repo_root=repo)
    from hermes_cli import outcome_verification as outcomes

    summary = outcomes.run_shadow_verifier(
        repo_root=repo,
        phase="canary",
        require_enabled=False,
        max_measurements=3,
    )
    print(_json(summary))
    return 0


def _run(
    command: Sequence[str], *, cwd: Path, timeout: int = 120, env: dict[str, str] | None = None
) -> dict[str, Any]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )
    return {
        "argv": list(command),
        "returncode": completed.returncode,
        "output_tail": completed.stdout[-1500:],
    }


def _write_e2e_worker_policy(
    state: Path,
    *,
    provider: str,
    model: str,
    worker_runtime: str,
    gate_command: Sequence[str],
) -> None:
    root_home = state / "home"
    profile = root_home / "profiles" / "coder"
    profile.mkdir(parents=True, exist_ok=True)
    profile_config: dict[str, Any] = {
        "worker_runtime": worker_runtime,
        "agent": {"max_turns": 30},
    }
    if worker_runtime == "claude-cli":
        profile_config["claude_model"] = model
    else:
        profile_config["model"] = {
            "provider": provider,
            "name": model,
            "default": model,
        }
    (profile / "config.yaml").write_text(
        json.dumps(profile_config, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (root_home / "config.yaml").write_text(
        json.dumps(
            {
                "kanban": {
                    "review_gate": {
                        "enabled": True,
                        "code_roles": ["coder"],
                        "auto_tier": False,
                        "standard_uses_llm_verifier": False,
                        "judge_at_chain_tip": True,
                    },
                    "worker_gate": {
                        "enabled": True,
                        "code_roles": ["coder"],
                        "default": [shlex.join(list(gate_command))],
                        "timeout": 900,
                    },
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    bin_dir = state / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    hermes = bin_dir / "hermes"
    hermes.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} -m hermes_cli.main \"$@\"\n",
        encoding="utf-8",
    )
    hermes.chmod(0o700)
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def e2e_canary(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(tempfile.mkdtemp(prefix="autoresearch-outcome-e2e-", dir="/tmp"))
    repo = state / "repo"
    candidate = _run(["git", "rev-parse", "HEAD"], cwd=root)
    candidate_sha = candidate["output_tail"].strip()
    if candidate["returncode"] != 0 or len(candidate_sha) != 40:
        raise RuntimeError("candidate checkout has no exact commit SHA")
    clone = _run(
        ["git", "clone", "--no-hardlinks", "--quiet", str(root), str(repo)],
        cwd=state,
        timeout=300,
    )
    checkout = _run(
        ["git", "checkout", "-q", "-B", "outcome-e2e-main", candidate_sha],
        cwd=repo,
    )
    _run(["git", "config", "user.name", "Codex Outcome E2E"], cwd=repo)
    _run(["git", "config", "user.email", "codex-outcome-e2e@localhost"], cwd=repo)
    cloned_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo)["output_tail"].strip()
    if clone["returncode"] or checkout["returncode"] or cloned_sha != candidate_sha:
        raise RuntimeError("temporary clone does not match the exact candidate SHA")

    fixtures = {
        "hermes_cli/outcome_e2e_improved.py": "def value():\n    return 0\n",
        "tests/hermes_cli/test_outcome_e2e_improved.py": (
            "from hermes_cli.outcome_e2e_improved import value\n\n"
            "def test_value():\n    assert value() == 1\n"
        ),
        "hermes_cli/outcome_e2e_counter.py": "def value():\n    return 0\n",
        "tests/hermes_cli/test_outcome_e2e_counter.py": (
            "from hermes_cli.outcome_e2e_counter import value\n\n"
            "def test_value():\n    assert value() == 1\n"
        ),
        "scripts/outcome-e2e-delivery.txt": "controlled delivery baseline\n",
    }
    for relative, content in fixtures.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run(["git", "add", "--", *fixtures], cwd=repo)
    fixture_commit = _run(
        ["git", "commit", "-q", "-m", "test: add controlled outcome e2e fixtures"],
        cwd=repo,
    )
    if fixture_commit["returncode"] != 0:
        raise RuntimeError("controlled fixture commit failed")

    # The exact source clone intentionally has no copied dependency tree.  The
    # canonical gate already supports a HOME-scoped shared venv, so expose the
    # interpreter that launched this harness through that documented seam.
    shared_venv = Path(sys.prefix)
    shared_venv_link = state / "home" / ".hermes" / "hermes-agent" / "venv"
    shared_venv_link.parent.mkdir(parents=True, exist_ok=True)
    shared_venv_link.symlink_to(shared_venv, target_is_directory=True)

    _configure(repo, state, repo_root=repo)
    from hermes_cli import autoresearch_proposals as proposals
    from hermes_cli import kanban_db as kb
    from hermes_cli import outcome_verification as outcomes

    kb.init_db()
    cases = [
        {
            "id": "e2e-improved",
            "target": "hermes_cli/outcome_e2e_improved.py",
            "test": "tests/hermes_cli/test_outcome_e2e_improved.py",
            "expected": "improved",
            "category": "bug_risk",
            "theme": "regression",
            "fix_hint": (
                "Change only hermes_cli/outcome_e2e_improved.py so value() returns 1; "
                "do not alter the regression test. Run the named focused test and commit."
            ),
            "gate": [
                "scripts/run_tests.sh",
                "tests/hermes_cli/test_outcome_e2e_improved.py",
            ],
        },
        {
            "id": "e2e-counter-worsened",
            "target": "hermes_cli/outcome_e2e_counter.py",
            "test": "tests/hermes_cli/test_outcome_e2e_counter.py",
            "expected": "worsened",
            "category": "bug_risk",
            "theme": "regression-counter",
            "fix_hint": (
                "In hermes_cli/outcome_e2e_counter.py set value() to return 1. Also add "
                "this exact multiline module string, using real newlines (never slash-"
                "separated text):\nCOUNTER_EVIDENCE = '''\ntry:\n    work()\nexcept "
                "Exception:\n    pass\n'''\nDo not alter the test. Run the focused test and "
                "commit the target-only change."
            ),
            "gate": [
                "scripts/run_tests.sh",
                "tests/hermes_cli/test_outcome_e2e_counter.py",
            ],
            "counter_patterns": [
                {
                    "path": "hermes_cli/outcome_e2e_counter.py",
                    "pattern_rule": "silent_except",
                }
            ],
        },
        {
            "id": "e2e-unmeasurable",
            "target": "scripts/outcome-e2e-delivery.txt",
            "test": None,
            "expected": "unmeasurable",
            "category": "delivery_proof",
            "theme": "delivery-only",
            "fix_hint": (
                "Append exactly 'delivered without a benefit proxy' as a new line to "
                "scripts/outcome-e2e-delivery.txt, run git diff --check, and commit."
            ),
            "gate": ["git", "diff", "--check"],
        },
    ]
    case_results: list[dict[str, Any]] = []
    repo_script = repo / "scripts" / SCRIPT.name
    for index, case in enumerate(cases):
        pre_delivery_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo)[
            "output_tail"
        ].strip()
        payload = {
            "id": case["id"],
            "schema": proposals.PROPOSAL_SCHEMA,
            "mode": "test" if case["test"] else "code",
            "proposal_type": "controlled_post_discovery_e2e",
            "finding_id": case["id"],
            "target": case["target"],
            "target_path": case["target"],
            "title": f"Controlled outcome case: {case['id']}",
            "category": case["category"],
            "theme": case["theme"],
            "severity": "low",
            "evidence": f"Grounded controlled defect at {case['target']}",
            "fix_hint": case["fix_hint"],
            "expected_benefit": "Evaluate only the preregistered deterministic probe.",
            "risk_summary": "Temporary exact-candidate clone only; never pushed.",
            "status": "proposed",
            "created_at": f"2026-07-15T01:0{index}:00Z",
        }
        if case["test"]:
            payload["affected_tests"] = [case["test"]]
        if case.get("counter_patterns"):
            payload["counter_patterns"] = case["counter_patterns"]
        _write_e2e_worker_policy(
            state,
            provider=str(args.worker_provider),
            model=str(args.worker_model),
            worker_runtime=str(args.worker_runtime),
            gate_command=case["gate"],
        )
        proposals.save_proposal(payload)

        reconcile_process = _run(
            [
                sys.executable,
                str(repo_script),
                "_e2e-reconcile-worker",
                "--root",
                str(repo),
                "--state",
                str(state),
                "--repo",
                str(repo),
                "--max-new",
                "1",
                "--min-severity",
                "low",
            ],
            cwd=repo,
            timeout=600,
        )
        if reconcile_process["returncode"] != 0:
            raise RuntimeError(f"separate reconcile failed: {reconcile_process}")
        reconcile_payload = json.loads(
            reconcile_process["output_tail"].strip().splitlines()[-1]
        )
        if (
            reconcile_payload.get("new_tasks") != 1
            or reconcile_payload.get("routed_to_kanban") != 1
            or reconcile_payload.get("errors") != 0
        ):
            raise RuntimeError(
                "reconcile did not route the controlled low-severity canary: "
                f"{reconcile_payload}"
            )
        with kb.connect() as conn:
            task = conn.execute(
                "SELECT t.id, t.status, t.workspace_kind, t.workspace_path, "
                "c.contract_id, c.contract_hash, c.contract_json, c.baseline_json "
                "FROM outcome_contracts c JOIN tasks t ON t.id=c.task_id "
                "WHERE c.proposal_id=?",
                (case["id"],),
            ).fetchone()
        if task is None or task["status"] != "ready":
            raise RuntimeError(f"reconcile did not release case {case['id']}")
        if task["workspace_kind"] != "worktree" or Path(task["workspace_path"]) != repo:
            raise RuntimeError("reconcile did not select dispatcher worktree isolation")
        task_id = str(task["id"])
        baseline = json.loads(task["baseline_json"])
        stored = proposals.load_proposal(str(case["id"])) or {}
        if (
            baseline.get("target_sha") != pre_delivery_sha
            or stored.get("outcome_target_sha") != pre_delivery_sha
        ):
            raise RuntimeError("proposal and contract baseline do not share the target SHA")

        dispatch_process = _run(
            [
                sys.executable,
                str(repo_script),
                "_e2e-dispatch-worker",
                "--root",
                str(repo),
                "--state",
                str(state),
                "--repo",
                str(repo),
                "--auth-home",
                str(Path(args.auth_home).resolve()),
            ],
            cwd=repo,
            timeout=1_500,
        )
        if dispatch_process["returncode"] != 0:
            raise RuntimeError(f"real dispatcher/worker failed: {dispatch_process}")
        dispatch_payload = json.loads(
            dispatch_process["output_tail"].strip().splitlines()[-1]
        )
        with kb.connect() as conn:
            delivered = conn.execute(
                "SELECT status, result FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            delivery_witnesses = conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id=? "
                "AND kind IN ('integration_merged', 'INTEGRATOR_VERIFIED') ORDER BY id",
                (task_id,),
            ).fetchall()
        if (
            not dispatch_payload.get("spawned")
            or delivered is None
            or delivered["status"] != "done"
            or len(delivery_witnesses) != 2
            or dispatch_payload.get("review_skipped_deterministic") != 1
        ):
            raise RuntimeError(
                "dispatcher did not produce a completed delivery with both "
                f"integrator witnesses: payload={dispatch_payload}, "
                f"task={dict(delivered) if delivered is not None else None}, "
                f"events={[row['kind'] for row in delivery_witnesses]}"
            )
        verifier_process = _run(
            [
                sys.executable,
                str(repo_script),
                "_e2e-verifier-worker",
                "--root",
                str(repo),
                "--state",
                str(state),
                "--repo",
                str(repo),
            ],
            cwd=repo,
            timeout=600,
        )
        if verifier_process["returncode"] != 0:
            raise RuntimeError(f"separate verifier failed: {verifier_process}")
        verifier = json.loads(verifier_process["output_tail"].strip().splitlines()[-1])
        with kb.connect() as conn:
            attempt = conn.execute(
                "SELECT status, verdict, integration_sha, observation_json, cost_usd, "
                "cost_breakdown_json, source_refs_json FROM outcome_attempts "
                "WHERE task_id=?",
                (task_id,),
            ).fetchone()
            integration_events = [
                {"kind": row["kind"], "payload": json.loads(row["payload"] or "{}")}
                for row in conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id=? "
                    "AND kind IN ('integration_merged', 'INTEGRATOR_VERIFIED') ORDER BY id",
                    (task_id,),
                ).fetchall()
            ]
            event_counts = {
                row["kind"]: int(row["n"])
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM task_events WHERE task_id=? "
                    "AND kind IN (?, ?, ?) GROUP BY kind",
                    (
                        task_id,
                        outcomes.CONTRACT_EVENT,
                        outcomes.MEASUREMENT_STARTED_EVENT,
                        outcomes.MEASUREMENT_COMPLETED_EVENT,
                    ),
                ).fetchall()
            }
        if attempt is None or attempt["verdict"] != case["expected"]:
            raise RuntimeError(
                f"case {case['id']} verdict mismatch: "
                f"{dict(attempt) if attempt is not None else None}"
            )
        merge_shas = {
            str(event["payload"].get("merge_commit") or "")
            for event in integration_events
        }
        if len(integration_events) != 2 or merge_shas != {attempt["integration_sha"]}:
            raise RuntimeError("real integrator witnesses do not match measured SHA")
        api = proposals.proposals_payload()
        card = next(item for item in api["proposals"] if item["id"] == case["id"])
        api_costs = {
            "actual_usd": card.get("outcome_cost_actual_usd"),
            "api_equivalent_usd": card.get("outcome_cost_api_equivalent_usd"),
            "effective_usd": card.get("outcome_cost_effective_usd"),
            "compatibility_effective_usd": card.get("outcome_cost_usd"),
            "status": card.get("outcome_cost_status"),
        }
        if api_costs["status"] != "complete" or any(
            api_costs[key] is None
            for key in ("actual_usd", "api_equivalent_usd", "effective_usd")
        ):
            raise RuntimeError("E2E API cost dimensions are incomplete")
        if abs(
            float(api_costs["actual_usd"])
            + float(api_costs["api_equivalent_usd"])
            - float(api_costs["effective_usd"])
        ) > 1e-8 or api_costs["compatibility_effective_usd"] != api_costs["effective_usd"]:
            raise RuntimeError("E2E API actual/equivalent/effective cost dimensions disagree")
        case_results.append(
            {
                "id": case["id"],
                "expected_verdict": case["expected"],
                "pre_delivery_sha": pre_delivery_sha,
                "task_id": task_id,
                "contract_id": task["contract_id"],
                "contract_hash": task["contract_hash"],
                "contract": json.loads(task["contract_json"]),
                "baseline": baseline,
                "reconcile_process": reconcile_process,
                "dispatch_process": dispatch_process,
                "worker_runs": dispatch_payload.get("worker_runs") or [],
                "provider_calls": int(dispatch_payload.get("provider_calls") or 0),
                "provider_cost_status": dispatch_payload.get("provider_cost_status"),
                "known_provider_cost_usd": float(
                    dispatch_payload.get("known_provider_cost_usd") or 0.0
                ),
                "provider_cost_usd": dispatch_payload.get("provider_cost_usd"),
                "provider_cost_usd_equivalent": dispatch_payload.get(
                    "provider_cost_usd_equivalent"
                ),
                "provider_effective_cost_usd": dispatch_payload.get(
                    "provider_effective_cost_usd"
                ),
                "verifier_process": verifier_process,
                "verifier": verifier,
                "integration_events": integration_events,
                "attempt": {
                    "status": attempt["status"],
                    "verdict": attempt["verdict"],
                    "integration_sha": attempt["integration_sha"],
                    "observation": json.loads(attempt["observation_json"]),
                    "cost_usd": float(attempt["cost_usd"]),
                    "cost_breakdown": json.loads(attempt["cost_breakdown_json"] or "{}"),
                    "source_refs": json.loads(attempt["source_refs_json"] or "[]"),
                },
                "event_counts": event_counts,
                "api": {
                    "measurement_status": card["measurement_status"],
                    "outcome_verdict": card["outcome_verdict"],
                    "evidence_grade": card["evidence_grade"],
                    "outcome_integration_sha": card["outcome_integration_sha"],
                    "costs": api_costs,
                },
            }
        )

    def _db_counts() -> dict[str, int]:
        with kb.connect() as conn:
            return {
                "tasks": int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
                "contracts": int(
                    conn.execute("SELECT COUNT(*) FROM outcome_contracts").fetchone()[0]
                ),
                "attempts": int(
                    conn.execute("SELECT COUNT(*) FROM outcome_attempts").fetchone()[0]
                ),
                "task_events": int(
                    conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
                ),
            }

    before_second_pass = _db_counts()
    second_reconcile = _run(
        [
            sys.executable,
            str(repo_script),
            "_e2e-reconcile-worker",
            "--root",
            str(repo),
            "--state",
            str(state),
            "--repo",
            str(repo),
            "--max-new",
            "5",
        ],
        cwd=repo,
        timeout=600,
    )
    second_verifier = _run(
        [
            sys.executable,
            str(repo_script),
            "_e2e-verifier-worker",
            "--root",
            str(repo),
            "--state",
            str(state),
            "--repo",
            str(repo),
        ],
        cwd=repo,
        timeout=600,
    )
    after_second_pass = _db_counts()
    if (
        second_reconcile["returncode"] != 0
        or second_verifier["returncode"] != 0
        or before_second_pass != after_second_pass
    ):
        raise RuntimeError("second reconcile/verifier pass was not idempotent")

    api = proposals.proposals_payload()
    result = {
        "mode": "post-discovery-e2e-canary",
        "candidate_root": str(root),
        "candidate_sha": candidate_sha,
        "cloned_candidate_sha": cloned_sha,
        "state": str(state),
        "repo": str(repo),
        "fixture_baseline_sha": case_results[0]["pre_delivery_sha"],
        "cases": case_results,
        "idempotence": {
            "before": before_second_pass,
            "after": after_second_pass,
            "second_reconcile": second_reconcile,
            "second_verifier": second_verifier,
        },
        "provider_calls": sum(item["provider_calls"] for item in case_results),
        "provider_cost_status": (
            "complete"
            if all(item["provider_cost_status"] == "complete" for item in case_results)
            else "partial"
        ),
        "known_provider_cost_usd": round(
            sum(item["known_provider_cost_usd"] for item in case_results), 8
        ),
        "provider_cost_usd": (
            round(sum(float(item["provider_cost_usd"]) for item in case_results), 8)
            if all(item["provider_cost_usd"] is not None for item in case_results)
            else None
        ),
        "provider_cost_usd_equivalent": (
            round(
                sum(float(item["provider_cost_usd_equivalent"]) for item in case_results),
                8,
            )
            if all(item["provider_cost_usd_equivalent"] is not None for item in case_results)
            else None
        ),
        "provider_effective_cost_usd": (
            round(
                sum(float(item["provider_effective_cost_usd"]) for item in case_results),
                8,
            )
            if all(item["provider_effective_cost_usd"] is not None for item in case_results)
            else None
        ),
        "outcome_metrics": api["metrics"]["outcomes"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    flood = sub.add_parser("flood", help="two-process reconcile flood replay")
    flood.add_argument("--root", required=True)
    flood.add_argument("--proposals", type=int, default=8)
    flood.add_argument("--max-new", type=int, default=5)
    flood.add_argument("--waves", type=int, default=1)
    flood.set_defaults(func=flood_replay)

    worker = sub.add_parser("_flood-worker")
    worker.add_argument("--root", required=True)
    worker.add_argument("--state", required=True)
    worker.add_argument("--start-at", required=True, type=float)
    worker.add_argument("--max-new", required=True, type=int)
    worker.set_defaults(func=flood_worker)

    lifecycle = sub.add_parser("lifecycle", help="replay lifecycle projection against a DB/proposal snapshot")
    lifecycle.add_argument("--root", required=True)
    lifecycle.add_argument("--proposals-dir", required=True)
    lifecycle.add_argument("--kanban-db", required=True)
    lifecycle.set_defaults(func=lifecycle_replay)

    flood_outcome = sub.add_parser(
        "flood-backtest", help="canonical parent/target flood outcome replay"
    )
    flood_outcome.add_argument("--parent-root", required=True)
    flood_outcome.add_argument("--target-root", required=True)
    flood_outcome.add_argument("--proposals", type=int, default=8)
    flood_outcome.add_argument("--max-new", type=int, default=5)
    flood_outcome.set_defaults(func=flood_backtest)

    lifecycle_outcome = sub.add_parser(
        "lifecycle-backtest", help="canonical parent/target lifecycle outcome replay"
    )
    lifecycle_outcome.add_argument("--parent-root", required=True)
    lifecycle_outcome.add_argument("--target-root", required=True)
    lifecycle_outcome.add_argument("--proposals-dir", required=True)
    lifecycle_outcome.add_argument("--kanban-db", required=True)
    lifecycle_outcome.set_defaults(func=lifecycle_backtest)

    e2e = sub.add_parser("e2e", help="real isolated post-discovery worker/gate/integration/verifier canary")
    e2e.add_argument("--root", required=True)
    e2e.add_argument("--auth-home", required=True)
    e2e.add_argument("--worker-provider", required=True)
    e2e.add_argument("--worker-model", required=True)
    e2e.add_argument(
        "--worker-runtime", choices=("hermes", "claude-cli"), default="hermes"
    )
    e2e.set_defaults(func=e2e_canary)

    e2e_reconcile = sub.add_parser("_e2e-reconcile-worker")
    e2e_reconcile.add_argument("--root", required=True)
    e2e_reconcile.add_argument("--state", required=True)
    e2e_reconcile.add_argument("--repo", required=True)
    e2e_reconcile.add_argument("--max-new", type=int, default=5)
    e2e_reconcile.add_argument("--min-severity", default="medium")
    e2e_reconcile.set_defaults(func=e2e_reconcile_worker)

    e2e_dispatch = sub.add_parser("_e2e-dispatch-worker")
    e2e_dispatch.add_argument("--root", required=True)
    e2e_dispatch.add_argument("--state", required=True)
    e2e_dispatch.add_argument("--repo", required=True)
    e2e_dispatch.add_argument("--auth-home", required=True)
    e2e_dispatch.set_defaults(func=e2e_dispatch_worker)

    e2e_verifier = sub.add_parser("_e2e-verifier-worker")
    e2e_verifier.add_argument("--root", required=True)
    e2e_verifier.add_argument("--state", required=True)
    e2e_verifier.add_argument("--repo", required=True)
    e2e_verifier.set_defaults(func=e2e_verifier_worker)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
