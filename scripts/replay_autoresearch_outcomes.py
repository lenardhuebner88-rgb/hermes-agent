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


def e2e_reconcile_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    repo = Path(args.repo).resolve()
    _configure(root, state, repo_root=repo)
    from hermes_cli import autoresearch_reconcile as reconcile

    reconcile.REPO_ROOT = repo
    summary = reconcile.reconcile_proposals(max_new_tasks=int(args.max_new))
    print(_json(summary))
    return 0


def e2e_code_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    repo = Path(args.repo).resolve()
    workspace = Path(args.workspace).resolve()
    task_id = str(args.task)
    _configure(root, state, repo_root=repo)
    from hermes_cli import autoresearch_proposals as proposals
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        row = conn.execute(
            "SELECT proposal_id FROM outcome_contracts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"task {task_id} has no registered outcome contract")
    proposal_id = str(row["proposal_id"])
    proposal = proposals.load_proposal(proposal_id)
    if not proposal:
        raise RuntimeError(f"proposal {proposal_id} is missing")

    changed: list[str]
    gate_command: list[str]
    if proposal_id == "e2e-improved":
        relative = "hermes_cli/outcome_e2e_improved.py"
        target = workspace / relative
        before = "def value():\n    return 0\n"
        after = "def value():\n    return 1\n"
        text = target.read_text(encoding="utf-8")
        if before not in text:
            raise RuntimeError("improved canary baseline is missing")
        target.write_text(text.replace(before, after), encoding="utf-8")
        changed = [relative]
        gate_command = [
            str(workspace / "scripts" / "run_tests.sh"),
            "tests/hermes_cli/test_outcome_e2e_improved.py",
        ]
    elif proposal_id == "e2e-counter-worsened":
        relative = "hermes_cli/outcome_e2e_counter.py"
        target = workspace / relative
        before = "def value():\n    return 0\n"
        after = (
            "def value():\n    return 1\n\n"
            "COUNTER_EVIDENCE = '''\n"
            "try:\n"
            "    work()\n"
            "except Exception:\n"
            "    pass\n"
            "'''\n"
        )
        text = target.read_text(encoding="utf-8")
        if before not in text:
            raise RuntimeError("counter canary baseline is missing")
        target.write_text(text.replace(before, after), encoding="utf-8")
        changed = [relative]
        gate_command = [
            str(workspace / "scripts" / "run_tests.sh"),
            "tests/hermes_cli/test_outcome_e2e_counter.py",
        ]
    elif proposal_id == "e2e-unmeasurable":
        relative = "scripts/outcome-e2e-delivery.txt"
        target = workspace / relative
        target.write_text(
            target.read_text(encoding="utf-8") + "delivered without a benefit proxy\n",
            encoding="utf-8",
        )
        changed = [relative]
        gate_command = ["git", "diff", "--check"]
    else:
        raise RuntimeError(f"unknown controlled canary proposal: {proposal_id}")

    worker_gate = _run(gate_command, cwd=workspace, timeout=600)
    if worker_gate["returncode"] != 0:
        raise RuntimeError(f"controlled worker gate failed: {worker_gate['output_tail']}")
    add = _run(["git", "add", "--", *changed], cwd=workspace)
    commit = _run(
        ["git", "commit", "-q", "-m", f"kanban({task_id}): {proposal_id}"],
        cwd=workspace,
    )
    sha = _run(["git", "rev-parse", "HEAD"], cwd=workspace)
    if add["returncode"] != 0 or commit["returncode"] != 0:
        raise RuntimeError(f"controlled worker commit failed: {commit['output_tail']}")

    with kb.connect() as conn:
        completed = kb.complete_task(
            conn,
            task_id,
            result=f"Controlled post-discovery delivery for {proposal_id}",
            summary=f"Delivered and gated {proposal_id}",
            metadata={
                "changed_files": changed,
                "tests_run": 1,
                "worker_gate": gate_command,
                "commit": sha["output_tail"].strip(),
                "provider_calls": 0,
                "cost_usd": 0.0,
                "disposition": {"items": []},
            },
            review_gate=False,
        )
        task = kb.get_task(conn, task_id)
        integration = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "AND kind IN ('integration_merged', 'INTEGRATOR_VERIFIED') ORDER BY id",
            (task_id,),
        ).fetchall()
    if not completed or task is None or task.status != "done":
        raise RuntimeError(f"real completion/integration did not finish task {task_id}")
    print(
        _json(
            {
                "ok": True,
                "proposal_id": proposal_id,
                "task_id": task_id,
                "workspace": str(workspace),
                "worker_commit": sha["output_tail"].strip(),
                "worker_gate": worker_gate,
                "integration_events": [
                    {"kind": item["kind"], "payload": json.loads(item["payload"] or "{}")}
                    for item in integration
                ],
                "provider_calls": 0,
                "cost_usd": 0.0,
            }
        )
    )
    return 0


def e2e_dispatch_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state = Path(args.state).resolve()
    repo = Path(args.repo).resolve()
    _configure(root, state, repo_root=repo)
    from hermes_cli import kanban_db as kb

    child_results: list[dict[str, Any]] = []

    def _spawn(task, workspace: str, *, board=None):  # noqa: ARG001
        env = dict(os.environ)
        env["HERMES_KANBAN_TASK"] = task.id
        env["HERMES_KANBAN_WORKSPACE"] = workspace
        if task.current_run_id is not None:
            env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
        if task.claim_lock:
            env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "_e2e-code-worker",
                "--root",
                str(root),
                "--state",
                str(state),
                "--repo",
                str(repo),
                "--task",
                task.id,
                "--workspace",
                workspace,
            ],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=1_200)
        parsed = _parse_worker(stdout, stderr, proc.returncode)
        child_results.append(parsed)
        if proc.returncode != 0:
            raise RuntimeError(f"controlled worker failed: {parsed}")
        return proc.pid

    with kb.connect() as conn:
        result = kb.dispatch_once(
            conn,
            spawn_fn=_spawn,
            max_spawn=1,
            max_in_progress=1,
            serialize_by_repo=True,
            max_concurrent_per_repo=1,
        )
    print(
        _json(
            {
                "spawned": [list(item) for item in result.spawned],
                "skipped_locked": bool(result.skipped_locked),
                "workers": child_results,
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
    # The production reconciler deliberately routes implementation cards to
    # the ``coder`` lane.  Mirror that real lane in the isolated HOME so the
    # dispatcher exercises its spawn/worktree path instead of correctly
    # rejecting the assignee as an unknown profile.
    coder_profile = state / "home" / "profiles" / "coder"
    coder_profile.mkdir(parents=True, exist_ok=True)
    (coder_profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")
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
        },
        {
            "id": "e2e-counter-worsened",
            "target": "hermes_cli/outcome_e2e_counter.py",
            "test": "tests/hermes_cli/test_outcome_e2e_counter.py",
            "expected": "worsened",
            "category": "bug_risk",
            "theme": "regression-counter",
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
            "severity": "high",
            "evidence": f"Grounded controlled defect at {case['target']}",
            "fix_hint": "Apply the narrow controlled change and pass the focused gate.",
            "expected_benefit": "Evaluate only the preregistered deterministic probe.",
            "risk_summary": "Temporary exact-candidate clone only; never pushed.",
            "status": "proposed",
            "created_at": f"2026-07-15T01:0{index}:00Z",
        }
        if case["test"]:
            payload["affected_tests"] = [case["test"]]
        if case.get("counter_patterns"):
            payload["counter_patterns"] = case["counter_patterns"]
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
            ],
            cwd=repo,
            timeout=600,
        )
        if reconcile_process["returncode"] != 0:
            raise RuntimeError(f"separate reconcile failed: {reconcile_process}")
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
        "provider_calls": 0,
        "provider_cost_usd": 0.0,
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

    e2e = sub.add_parser("e2e", help="real isolated post-discovery worker/gate/integration/verifier canary")
    e2e.add_argument("--root", required=True)
    e2e.set_defaults(func=e2e_canary)

    e2e_reconcile = sub.add_parser("_e2e-reconcile-worker")
    e2e_reconcile.add_argument("--root", required=True)
    e2e_reconcile.add_argument("--state", required=True)
    e2e_reconcile.add_argument("--repo", required=True)
    e2e_reconcile.add_argument("--max-new", type=int, default=5)
    e2e_reconcile.set_defaults(func=e2e_reconcile_worker)

    e2e_dispatch = sub.add_parser("_e2e-dispatch-worker")
    e2e_dispatch.add_argument("--root", required=True)
    e2e_dispatch.add_argument("--state", required=True)
    e2e_dispatch.add_argument("--repo", required=True)
    e2e_dispatch.set_defaults(func=e2e_dispatch_worker)

    e2e_worker = sub.add_parser("_e2e-code-worker")
    e2e_worker.add_argument("--root", required=True)
    e2e_worker.add_argument("--state", required=True)
    e2e_worker.add_argument("--repo", required=True)
    e2e_worker.add_argument("--task", required=True)
    e2e_worker.add_argument("--workspace", required=True)
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
