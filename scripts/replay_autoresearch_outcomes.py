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
    for index in range(count):
        proposal_id = f"replay-flood-{index:02d}"
        proposals.save_proposal(
            {
                "id": proposal_id,
                "schema": proposals.PROPOSAL_SCHEMA,
                "mode": "code",
                "proposal_type": "deep_audit",
                "finding_id": proposal_id,
                "target": f"hermes_cli/replay_{index:02d}.py",
                "target_path": f"hermes_cli/replay_{index:02d}.py",
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


def e2e_reconcile_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    repo = Path(args.repo).resolve()
    _configure(root, state, repo_root=repo)
    from hermes_cli import autoresearch_reconcile as reconcile

    reconcile.REPO_ROOT = repo
    summary = reconcile.reconcile_proposals(max_new_tasks=5)
    print(_json(summary))
    return 0


def e2e_code_worker(args: argparse.Namespace) -> int:
    target = Path(args.repo).resolve() / "hermes_cli" / "example.py"
    text = target.read_text(encoding="utf-8")
    old = "    except Exception:\n        pass\n    return None\n"
    new = "    except Exception:\n        return 1\n"
    if old not in text:
        raise RuntimeError("expected canary failure shape missing")
    target.write_text(text.replace(old, new), encoding="utf-8")
    print(_json({"ok": True, "changed": str(target)}))
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


def _run(command: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=120,
    )
    return {
        "argv": list(command),
        "returncode": completed.returncode,
        "output_tail": completed.stdout[-1500:],
    }


def e2e_canary(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(tempfile.mkdtemp(prefix="autoresearch-outcome-e2e-", dir="/tmp"))
    repo = state / "repo"
    (repo / "hermes_cli").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "hermes_cli" / "example.py").write_text(
        "def value():\n"
        "    try:\n"
        "        raise RuntimeError('grounded canary')\n"
        "    except Exception:\n"
        "        pass\n"
        "    return None\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_example.py").write_text(
        "from hermes_cli.example import value\n\n"
        "def test_value_recovers():\n"
        "    assert value() == 1\n",
        encoding="utf-8",
    )
    git_init = _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.name", "Codex Replay"], cwd=repo)
    _run(["git", "config", "user.email", "codex-replay@localhost"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    baseline_commit = _run(["git", "commit", "-q", "-m", "baseline canary"], cwd=repo)
    if git_init["returncode"] or baseline_commit["returncode"]:
        raise RuntimeError("local canary git initialization failed")

    _configure(root, state, repo_root=repo)
    from hermes_cli import autoresearch_proposals as proposals
    from hermes_cli import kanban_db as kb
    from hermes_cli import outcome_verification as outcomes

    kb.init_db()
    proposals.save_proposal(
        {
            "id": "post-discovery-canary",
            "schema": proposals.PROPOSAL_SCHEMA,
            "mode": "test",
            "proposal_type": "mutation_test",
            "finding_id": "post-discovery-canary",
            "target": "hermes_cli/example.py",
            "target_path": "hermes_cli/example.py",
            "title": "Recover the grounded canary failure",
            "category": "bug_risk",
            "theme": "silent-except",
            "severity": "high",
            "evidence": "value() returns None after the caught RuntimeError",
            "fix_hint": "Return the required bounded recovery value.",
            "affected_tests": ["tests/test_example.py"],
            "expected_benefit": "The focused regression changes from failing to passing.",
            "risk_summary": "Isolated temporary repository only.",
            "status": "proposed",
            "created_at": "2026-07-15T00:00:00Z",
        }
    )

    reconcile_process = _run(
        [
            sys.executable,
            str(SCRIPT),
            "_e2e-reconcile-worker",
            "--root",
            str(root),
            "--state",
            str(state),
            "--repo",
            str(repo),
        ],
        cwd=root,
    )
    with kb.connect() as conn:
        task = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ?",
            ("autoresearch:test-foundry:hermes-cli-example.py",),
        ).fetchone()
        if task is None or task["status"] != "ready":
            raise RuntimeError("reconcile did not release the contracted canary task")
        task_id = str(task["id"])
        contract_row = conn.execute(
            "SELECT contract_id, contract_hash, baseline_json FROM outcome_contracts WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    worker_process = _run(
        [sys.executable, str(SCRIPT), "_e2e-code-worker", "--repo", str(repo)],
        cwd=repo,
    )
    gate_process = _run([sys.executable, "-m", "pytest", "-q", "tests/test_example.py"], cwd=repo)
    if worker_process["returncode"] != 0 or gate_process["returncode"] != 0:
        raise RuntimeError("worker or focused gate failed")
    _run(["git", "add", "hermes_cli/example.py"], cwd=repo)
    integration_commit = _run(["git", "commit", "-q", "-m", "codex: integrate outcome canary"], cwd=repo)
    sha_process = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    integration_sha = sha_process["output_tail"].strip()
    if integration_commit["returncode"] != 0 or len(integration_sha) != 40:
        raise RuntimeError("local canary integration commit failed")

    with kb.connect() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "integration_merged", {"commit_sha": integration_sha})
            kb._append_event(conn, task_id, "INTEGRATOR_VERIFIED", {"commit_sha": integration_sha})
    verifier_process = _run(
        [
            sys.executable,
            str(SCRIPT),
            "_e2e-verifier-worker",
            "--root",
            str(root),
            "--state",
            str(state),
            "--repo",
            str(repo),
        ],
        cwd=root,
    )
    if verifier_process["returncode"] != 0:
        raise RuntimeError("separate verifier process failed")
    verifier = json.loads(verifier_process["output_tail"].strip().splitlines()[-1])
    with kb.connect() as conn:
        attempt = conn.execute(
            "SELECT status, verdict, integration_sha, observation_json, cost_usd "
            "FROM outcome_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        event_counts = {
            row["kind"]: int(row["n"])
            for row in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM task_events WHERE task_id = ? "
                "AND kind IN (?, ?, ?) GROUP BY kind",
                (
                    task_id,
                    outcomes.CONTRACT_EVENT,
                    outcomes.MEASUREMENT_STARTED_EVENT,
                    outcomes.MEASUREMENT_COMPLETED_EVENT,
                ),
            ).fetchall()
        }
    api = proposals.proposals_payload()
    card = next(item for item in api["proposals"] if item["id"] == "post-discovery-canary")
    result = {
        "mode": "post-discovery-e2e-canary",
        "root": str(root),
        "state": str(state),
        "repo": str(repo),
        "reconcile_process": reconcile_process,
        "worker_process": worker_process,
        "gate_process": gate_process,
        "verifier_process": verifier_process,
        "task_id": task_id,
        "task_status_after_contract_release": "ready",
        "contract": {
            "contract_id": contract_row["contract_id"],
            "contract_hash": contract_row["contract_hash"],
            "baseline": json.loads(contract_row["baseline_json"]),
        },
        "integration_sha": integration_sha,
        "verifier": verifier,
        "attempt": {
            "status": attempt["status"],
            "verdict": attempt["verdict"],
            "integration_sha": attempt["integration_sha"],
            "observation": json.loads(attempt["observation_json"]),
            "cost_usd": float(attempt["cost_usd"]),
        },
        "event_counts": event_counts,
        "api": {
            "measurement_status": card["measurement_status"],
            "outcome_verdict": card["outcome_verdict"],
            "evidence_grade": card["evidence_grade"],
            "outcome_integration_sha": card["outcome_integration_sha"],
            "outcome_metrics": api["metrics"]["outcomes"],
        },
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

    e2e = sub.add_parser("e2e", help="real isolated post-discovery worker/gate/integration/verifier canary")
    e2e.add_argument("--root", required=True)
    e2e.set_defaults(func=e2e_canary)

    e2e_reconcile = sub.add_parser("_e2e-reconcile-worker")
    e2e_reconcile.add_argument("--root", required=True)
    e2e_reconcile.add_argument("--state", required=True)
    e2e_reconcile.add_argument("--repo", required=True)
    e2e_reconcile.set_defaults(func=e2e_reconcile_worker)

    e2e_worker = sub.add_parser("_e2e-code-worker")
    e2e_worker.add_argument("--repo", required=True)
    e2e_worker.set_defaults(func=e2e_code_worker)

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
