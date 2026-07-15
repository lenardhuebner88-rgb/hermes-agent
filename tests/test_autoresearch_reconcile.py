"""Tests for the Autoresearch proposal reconciler.

The reconciler is the bridge from passive proposals to the self-improvement
flywheel: skill-doc fixes are applied behind the existing gate, code/test findings
become deduped Kanban work, and risky/no-diff findings become operator decisions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest

from hermes_cli import autoresearch_proposals as proposals
from hermes_cli import kanban_db as kb


@pytest.fixture()
def reconcile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    audit = home / "skill-audit"
    digest = home / "state" / "strategist" / "autoresearch-digest.json"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(audit))
    monkeypatch.setenv("HERMES_AUTORESEARCH_DIGEST_PATH", str(digest))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    repo = tmp_path / "repo"
    for name in ("a.py", "auth.py", "dispatcher.py", "example.py", "utils.py"):
        path = repo / "hermes_cli" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "try:\n    work()\nexcept Exception:\n    pass\n",
            encoding="utf-8",
        )
    test_path = repo / "tests" / "test_auth.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("def test_auth():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Reconcile Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "reconcile@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    from hermes_cli import autoresearch_reconcile as reconcile

    monkeypatch.setattr(reconcile, "REPO_ROOT", repo)
    return {"home": home, "audit": audit, "digest": digest, "repo": repo}


def _proposal(pid: str, **overrides):
    data = {
        "id": pid,
        "schema": proposals.PROPOSAL_SCHEMA,
        "mode": "skill",
        "target": "demo",
        "target_path": "/tmp/demo/SKILL.md",
        "section": "Output",
        "eval_label": "Output / Ergebnis",
        "category": "missing_section",
        "severity": "high",
        "finding_id": pid,
        "subsystem": "skills",
        "theme": "missing-section",
        "title": f"Proposal {pid}",
        "rationale_plain": "test rationale",
        "evidence": "raise RuntimeError('grounded example')",
        "fix_hint": "Handle the grounded failure explicitly.",
        "before_text": "before",
        "after_text": "after",
        "new_text": "new",
        "diff_before_after": "--- a/demo\n+++ b/demo\n@@ -1 +1 @@\n-before\n+after",
        "status": "proposed",
        "created_at": "2026-06-21T00:00:00Z",
        "applied_at": None,
        "result": None,
    }
    data.update(overrides)
    proposals.save_proposal(data)
    return data


def _load(pid: str) -> dict:
    loaded = proposals.load_proposal(pid)
    assert loaded is not None
    return loaded


def test_skill_doc_with_diff_is_held_for_real_judge(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal("skill-fix")
    monkeypatch.setattr(
        reconcile.proposals,
        "apply_proposal",
        lambda *_a, **_k: pytest.fail("reconciler must never impersonate the independent judge"),
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)

    assert summary["applied"] == 0
    assert summary["held_judge_required"] == 1
    held = _load("skill-fix")
    assert held["status"] == "proposed"
    assert held["last_outcome"] == "held_judge_required"
    digest = json.loads(reconcile_env["digest"].read_text(encoding="utf-8"))
    assert digest["themes"][0]["count"] == 1


def test_repeated_reconcile_keeps_skill_judge_hold_idempotent(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal("skill-reverts", category="silent_except", theme="silent-except")
    monkeypatch.setattr(
        reconcile.proposals,
        "apply_proposal",
        lambda *_a, **_k: pytest.fail("reconciler must not apply held skill proposals"),
    )

    with kb.connect() as conn:
        first = reconcile.reconcile_proposals(conn=conn)
        second = reconcile.reconcile_proposals(conn=conn)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert first["held_judge_required"] == 1
    assert second["held_judge_required"] == 1
    assert task_count == 0
    assert _load("skill-reverts")["status"] == "proposed"


def test_high_severity_code_findings_create_one_deduped_kanban_task(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "code-a",
        mode="code",
        finding_id="F-123",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Auth silent exception",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
    )
    _proposal(
        "code-b",
        mode="code",
        finding_id="F-123",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Duplicate Auth silent exception",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        rows = conn.execute(
            "SELECT id, title, body, acceptance_criteria, assignee, idempotency_key, status, "
            "review_tier, scope_contract, workspace_kind, workspace_path FROM tasks"
        ).fetchall()
        outcome_events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind IN (?, ?) ORDER BY id",
            (rows[0]["id"], "outcome_contract_registered", "unblocked"),
        ).fetchall()

    assert summary["routed_to_kanban"] == 1
    assert summary["stale"] == 1  # duplicate is retained as history, never delivered twice
    assert summary["new_tasks"] == 1
    assert len(rows) == 1
    assert rows[0]["assignee"] == "coder"
    assert rows[0]["idempotency_key"] == "autoresearch:F-123"
    assert rows[0]["review_tier"] == "review"
    assert rows[0]["status"] == "ready"
    assert rows[0]["workspace_kind"] == "worktree"
    assert rows[0]["workspace_path"] == str(reconcile_env["repo"])
    assert json.loads(rows[0]["acceptance_criteria"])
    assert "AC-AR1" in rows[0]["body"]
    contract = json.loads(rows[0]["scope_contract"])
    assert contract["source"] == "autoresearch"
    assert contract["proposal_id"] in {"code-a", "code-b"}
    assert contract["evidence"] == "raise RuntimeError('grounded example')"
    assert contract["allowed_paths"] == ["hermes_cli/auth.py"]
    stored = [_load("code-a"), _load("code-b")]
    routed = [item for item in stored if item["status"] == "routed_to_kanban"]
    duplicate = [item for item in stored if item["status"] == "skipped"]
    assert len(routed) == len(duplicate) == 1
    assert routed[0]["kanban_task_id"] == rows[0]["id"]
    assert duplicate[0]["last_outcome"] == "rejected_duplicate"
    assert routed[0]["measurement_status"] == "pending"
    assert routed[0]["evidence_grade"] == "legacy_observational"
    assert routed[0]["outcome_baseline_recorded_at"]
    assert routed[0]["probe_contract"]["contract_hash"]
    assert [row["kind"] for row in outcome_events] == ["outcome_contract_registered", "unblocked"]


def test_contract_failure_leaves_task_blocked_and_next_run_recovers(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "code-recover",
        mode="code",
        finding_id="F-RECOVER",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        category="bug_risk",
        theme="silent-except",
    )
    real_register = reconcile.outcomes.register_contract
    monkeypatch.setattr(
        reconcile.outcomes,
        "register_contract",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("crash after create")),
    )
    with kb.connect() as conn:
        first = reconcile.reconcile_proposals(conn=conn)
        row = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ?", ("autoresearch:F-RECOVER",)
        ).fetchone()
    assert first["errors"] == 1
    assert row["status"] == "blocked"
    assert _load("code-recover")["outcome_baseline_recorded_at"]

    monkeypatch.setattr(reconcile.outcomes, "register_contract", real_register)
    with kb.connect() as conn:
        second = reconcile.reconcile_proposals(conn=conn)
        recovered = conn.execute("SELECT status FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        contract_count = conn.execute("SELECT COUNT(*) FROM outcome_contracts").fetchone()[0]
    assert second["errors"] == 0
    assert second["new_tasks"] == 0
    assert recovered["status"] == "ready"
    assert contract_count == 1


def test_invalid_baseline_never_creates_a_dispatchable_or_blocked_task(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "code-missing-baseline",
        mode="code",
        finding_id="F-MISSING-BASELINE",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        category="bug_risk",
        theme="silent-except",
    )
    (reconcile_env["repo"] / "hermes_cli" / "auth.py").unlink()
    with kb.connect() as conn:
        result = reconcile.reconcile_proposals(conn=conn)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert result["errors"] == 1
    assert task_count == 0
    assert "outcome_baseline" not in _load("code-missing-baseline")


def test_code_finding_missing_grounded_contract_is_held(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "code-invalid",
        mode="code",
        finding_id="F-INVALID",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        evidence="",
        fix_hint="",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert summary["held_invalid_contract"] == 1
    assert task_count == 0
    held = _load("code-invalid")
    assert held["status"] == "skipped"
    assert held["last_outcome"] == "held_invalid_contract"
    assert "evidence" in held["result"] and "fix_hint" in held["result"]




def test_critical_severity_proposal_routes_with_max_iterations_220(reconcile_env):
    """A critical-severity code finding must be created with max_iterations=220."""
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "code-critical",
        mode="code",
        finding_id="F-CRIT",
        target="hermes_cli/dispatcher.py",
        target_path="hermes_cli/dispatcher.py",
        title="Critical data loss risk",
        category="bug_risk",
        theme="data-loss",
        subsystem="dispatcher",
        severity="critical",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        rows = conn.execute(
            "SELECT id, max_iterations, review_tier FROM tasks WHERE idempotency_key = ?",
            ("autoresearch:F-CRIT",),
        ).fetchall()

    assert summary["routed_to_kanban"] == 1
    assert summary["new_tasks"] == 1
    assert len(rows) == 1
    assert rows[0]["max_iterations"] == 220, (
        f"Expected max_iterations=220 for critical severity, got {rows[0]['max_iterations']}"
    )
    assert rows[0]["review_tier"] == "critical"


def test_existing_autoresearch_task_gets_missing_review_tier_backfilled(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    with kb.connect() as conn:
        existing_id = kb.create_task(
            conn,
            title="Existing Autoresearch finding",
            assignee="coder",
            created_by="autoresearch",
            idempotency_key="autoresearch:F-EXISTING",
            kind="code",
        )

    _proposal(
        "code-existing",
        mode="code",
        finding_id="F-EXISTING",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Existing Auth finding",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
        severity="high",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        row = conn.execute(
            "SELECT id, review_tier FROM tasks WHERE idempotency_key = ?",
            ("autoresearch:F-EXISTING",),
        ).fetchone()

    assert summary["routed_to_kanban"] == 1
    assert summary["new_tasks"] == 0
    assert row["id"] == existing_id
    assert row["review_tier"] == "review"


def test_running_autoresearch_task_contract_is_not_changed_mid_run(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    with kb.connect() as conn:
        existing_id = kb.create_task(
            conn,
            title="Active old Autoresearch finding",
            body="legacy body",
            assignee=None,
            created_by="autoresearch",
            idempotency_key="autoresearch:F-ACTIVE",
            kind="code",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (existing_id,))

    _proposal(
        "code-active",
        mode="code",
        finding_id="F-ACTIVE",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Active Auth finding",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
        severity="high",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        row = conn.execute(
            "SELECT body, acceptance_criteria, scope_contract FROM tasks WHERE id = ?",
            (existing_id,),
        ).fetchone()

    assert summary["routed_to_kanban"] == 1
    assert row["body"] == "legacy body"
    assert row["acceptance_criteria"] is None
    assert row["scope_contract"] is None


def test_low_severity_proposal_routes_with_max_iterations_none(reconcile_env):
    """A low-severity code finding must be created with max_iterations=None (inherits profile default)."""
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "code-low",
        mode="code",
        finding_id="F-LOW",
        target="hermes_cli/utils.py",
        target_path="hermes_cli/utils.py",
        title="Minor style issue",
        category="style",
        theme="style-nit",
        subsystem="utils",
        severity="low",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn, min_task_severity="low")
        rows = conn.execute(
            "SELECT id, max_iterations, review_tier FROM tasks WHERE idempotency_key = ?",
            ("autoresearch:F-LOW",),
        ).fetchall()

    assert summary["routed_to_kanban"] == 1
    assert summary["new_tasks"] == 1
    assert len(rows) == 1
    assert rows[0]["max_iterations"] is None, (
        f"Expected max_iterations=None for low severity, got {rows[0]['max_iterations']}"
    )
    assert rows[0]["review_tier"] is None



def test_vetoed_autoresearch_signal_is_suppressed(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    veto_path = reconcile_env["home"] / "state" / "strategist" / "vetoed_levers.json"
    veto_path.parent.mkdir(parents=True, exist_ok=True)
    veto_path.write_text(json.dumps(["autoresearch:silent-except"]), encoding="utf-8")
    _proposal(
        "code-suppressed",
        mode="code",
        finding_id="F-SUP",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Suppressed silent exception",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert summary["suppressed"] == 1
    assert task_count == 0
    routed = _load("code-suppressed")
    assert routed["status"] == "skipped"
    assert "silent-except" in routed["result"]


def test_flood_guard_caps_new_tasks_and_pools_the_rest(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    for i in range(60):
        _proposal(
            f"code-{i:02d}",
            mode="code",
            finding_id=f"F-{i:02d}",
            target="hermes_cli/example.py",
            target_path="hermes_cli/example.py",
            title=f"Finding {i}",
            category="bug_risk",
            theme="silent-except",
            subsystem="auth",
        )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn, max_new_tasks=5)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert task_count == 5
    assert summary["new_tasks"] == 5
    assert summary["pooled"] == 55
    assert len([p for p in proposals.list_proposals() if p.get("status") == "routed_to_kanban"]) == 5
    assert len([p for p in proposals.list_proposals() if p.get("status") == "pooled"]) == 55


def test_cross_process_reconcile_lock_owns_the_whole_flood_budget(reconcile_env, tmp_path):
    from hermes_cli import autoresearch_reconcile as reconcile

    for i in range(10):
        _proposal(
            f"race-{i:02d}",
            mode="code",
            finding_id=f"RACE-{i:02d}",
            target="hermes_cli/example.py",
            target_path="hermes_cli/example.py",
            category="bug_risk",
        )

    ready = tmp_path / "reconcile-lock-ready"
    release = tmp_path / "reconcile-lock-release"
    holder = tmp_path / "hold-reconcile-lock.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import time
            from pathlib import Path
            from hermes_cli import autoresearch_reconcile as reconcile

            handle = reconcile._try_acquire_reconcile_lock()
            assert handle is not None
            Path({str(ready)!r}).write_text("ready", encoding="utf-8")
            try:
                for _ in range(1000):
                    if Path({str(release)!r}).exists():
                        break
                    time.sleep(0.01)
            finally:
                reconcile._release_reconcile_lock(handle)
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    child = subprocess.Popen([sys.executable, str(holder)], env=env)
    try:
        for _ in range(1000):
            if ready.exists():
                break
            if child.poll() is not None:
                break
            time.sleep(0.01)
        assert ready.exists(), "child never acquired the reconcile lock"

        with kb.connect() as conn:
            busy = reconcile.reconcile_proposals(conn=conn, max_new_tasks=5)
            task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

        assert busy["ok"] is True
        assert busy["busy"] is True
        assert busy["new_tasks"] == 0
        assert task_count == 0
        assert all(item["status"] == "proposed" for item in proposals.list_proposals())
    finally:
        release.write_text("release", encoding="utf-8")
        child.wait(timeout=15)

    with kb.connect() as conn:
        routed = reconcile.reconcile_proposals(conn=conn, max_new_tasks=5)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert routed["busy"] is False
    assert routed["new_tasks"] == 5
    assert task_count == 5
    assert len([p for p in proposals.list_proposals() if p.get("status") == "pooled"]) == 5


def test_diff_less_finding_is_detection_only_and_digest_groups_theme(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "diff-less",
        mode="skill",
        diff_before_after="",
        new_text="",
        category="silent_except",
        theme="silent-except",
        subsystem="auth",
        severity="medium",
        finding_id="F-diff-less",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        queue = kb.decision_queue(conn)

    assert summary["rejected_detection_only"] == 1
    assert _load("diff-less")["status"] == "skipped"
    assert _load("diff-less")["operator_action_required"] is False
    assert queue["count"] == 0

    digest = json.loads(reconcile_env["digest"].read_text(encoding="utf-8"))
    assert digest["themes"] == [
        {
            "subsystem": "auth",
            "theme": "silent-except",
            "count": 1,
            "severity_max": "medium",
            "example_finding_ids": ["F-diff-less"],
            "atomic_tasks_filed": 0,
            "escalated": 0,
        }
    ]


def test_once_mode_uses_same_reconciler_without_running_drain(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    calls: list[dict] = []
    monkeypatch.setattr(reconcile, "reconcile_proposals", lambda **kw: calls.append(kw) or {"ok": True})

    rc = reconcile.main(["--once", "--max-new-tasks", "3"])

    assert rc == 0
    assert calls and calls[0]["once"] is True
    assert calls[0]["max_new_tasks"] == 3


def test_veto_operator_escalation_archives_and_records_via_real_path(reconcile_env):
    """The sanctioned veto path archives an Autoresearch escalation AND writes
    the ``freigabe_vetoed`` event that the strategist's reflect joins on —
    closing Naht 3 without a raw event injection."""
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "diff-less-veto", mode="skill", diff_before_after="", new_text="",
        category="silent_except", theme="silent-except", subsystem="auth",
        severity="high", finding_id="F-veto",
    )

    with kb.connect() as conn:
        task_id = reconcile._escalate(conn, _load("diff-less-veto"), "operator review required")

        vetoed = kb.veto_operator_escalation(conn, task_id, author="operator")

        assert vetoed is True
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert status == "archived"
        veto_events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id = ? AND kind = 'freigabe_vetoed'",
            (task_id,),
        ).fetchone()["n"]
        assert veto_events == 1


def test_reconcile_persists_last_summary_for_the_tab(reconcile_env, monkeypatch):
    """A real reconcile run records its outcome so the tab can show 'what the
    loop did last night'. Dry-run must NOT overwrite that record."""
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal("s1")  # one skill-doc-with-diff
    monkeypatch.setattr(
        reconcile.proposals, "apply_proposal",
        lambda *_a, **_k: pytest.fail("reconciler must not invoke apply_proposal"),
    )

    with kb.connect() as conn:
        reconcile.reconcile_proposals(conn=conn)

    rec = reconcile.load_last_reconcile()
    assert rec is not None
    assert rec["summary"]["held_judge_required"] == 1
    assert rec["summary"]["seen"] == 1
    assert isinstance(rec.get("generated_at"), str) and rec["generated_at"]
    assert isinstance(rec.get("themes"), list)

    # a later dry-run preview must not clobber the real record
    before = reconcile.load_last_reconcile()
    with kb.connect() as conn:
        reconcile.reconcile_proposals(conn=conn, dry_run=True)
    assert reconcile.load_last_reconcile() == before


def test_escalations_coalesce_by_signal(reconcile_env):
    """Many findings sharing a signal collapse into ONE operator escalation —
    the operator vetoes the signal, not each finding. Prevents a backlog drain
    from flooding the decision-queue (41 silent-except findings → 1 decision)."""
    from hermes_cli import autoresearch_reconcile as reconcile

    for i in range(5):
        _proposal(
            f"dl-{i}", mode="skill", diff_before_after="", new_text="",
            category="silent_except", theme="silent-except", subsystem="auth",
            severity="medium", finding_id=f"F-{i}",
        )

    with kb.connect() as conn:
        ids = [reconcile._escalate(conn, _load(f"dl-{i}"), "operator review required") for i in range(5)]
        esc_tasks = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE kind = 'ops'"
        ).fetchone()["n"]
        esc_events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE kind = ?",
            (kb.OPERATOR_ESCALATION_EVENT,),
        ).fetchone()["n"]
        queue = kb.decision_queue(conn)
        comments = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (ids[0],)
        ).fetchall()

    assert esc_tasks == 1                 # but ONE escalation task (coalesced by signal)
    assert esc_events == 1                # and one operator_escalation event
    assert queue["count"] == 1            # one decision-queue row, not five
    assert len(set(ids)) == 1
    assert len(comments) == 4
    assert all(f"[autoresearch-proposal:dl-{i}]" in "\n".join(row["body"] for row in comments) for i in range(1, 5))


def test_distinct_signals_do_not_coalesce(reconcile_env):
    """Findings with different signals stay separate escalations — coalescing
    is per-signal, not a blanket cap."""
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal("a", mode="skill", diff_before_after="", new_text="", category="silent_except",
              theme="silent-except", subsystem="auth", severity="medium", finding_id="FA")
    _proposal("b", mode="skill", diff_before_after="", new_text="", category="bare_except",
              theme="bare-except", subsystem="auth", severity="medium", finding_id="FB")

    with kb.connect() as conn:
        reconcile._escalate(conn, _load("a"), "operator review required")
        reconcile._escalate(conn, _load("b"), "operator review required")
        esc_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind = 'ops'").fetchone()["n"]

    assert esc_tasks == 2


def test_dry_run_classifies_without_side_effects(reconcile_env, monkeypatch):
    """--dry-run reports how the backlog WOULD route — no apply, no tasks, no
    proposal-status writes. Lets the operator preview the drain before it runs."""
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal("skill-x")  # skill-doc with diff → would wait for the real judge
    _proposal(
        "code-x", mode="code", finding_id="F-X", target="hermes_cli/a.py",
        target_path="hermes_cli/a.py", category="bug_risk", theme="silent-except",
        subsystem="auth",
    )  # code finding → would route to kanban
    _proposal(
        "diffless-x", mode="skill", diff_before_after="", new_text="",
        category="silent_except", theme="t", subsystem="auth", severity="medium",
        finding_id="F-D",
    )  # no actionable diff → detection-only, not an operator decision

    called: list[int] = []
    monkeypatch.setattr(reconcile.proposals, "apply_proposal", lambda *a, **k: called.append(1))

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn, dry_run=True)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert called == []           # apply_proposal never touched
    assert task_count == 0        # no kanban tasks / escalations created
    assert summary["dry_run"] is True
    assert summary["seen"] == 3
    assert summary["applied"] == 0
    assert summary["held_judge_required"] == 1
    assert summary["routed_to_kanban"] == 1
    assert summary["escalated"] == 0
    assert summary["rejected_detection_only"] == 1
    # proposals untouched on disk
    assert _load("skill-x")["status"] == "proposed"
    assert _load("code-x")["status"] == "proposed"
    assert _load("diffless-x")["status"] == "proposed"
    # no digest written in dry-run
    assert not reconcile_env["digest"].exists()


def test_pooled_findings_reenter_and_drain_on_the_next_run(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    for i in range(2):
        _proposal(
            f"drain-{i}", mode="code", finding_id=f"F-DRAIN-{i}",
            target="hermes_cli/auth.py", target_path="hermes_cli/auth.py",
            category="bug_risk", severity="high",
        )
    with kb.connect() as conn:
        first = reconcile.reconcile_proposals(conn=conn, max_new_tasks=1)
        assert first["new_tasks"] == 1
        assert first["pooled"] == 1
        pooled_id = next(item["id"] for item in proposals.list_proposals() if item["status"] == "pooled")

        second = reconcile.reconcile_proposals(conn=conn, max_new_tasks=1)

    assert second["seen"] == 1
    assert second["new_tasks"] == 1
    assert second["routed_to_kanban"] == 1
    assert _load(pooled_id)["status"] == "routed_to_kanban"


def test_payload_syncs_terminal_kanban_task_out_of_open_delivery(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "terminal", mode="code", finding_id="F-TERMINAL",
        target="hermes_cli/auth.py", target_path="hermes_cli/auth.py",
        category="bug_risk", severity="high",
    )
    with kb.connect() as conn:
        reconcile.reconcile_proposals(conn=conn)
        task_id = _load("terminal")["kanban_task_id"]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (1_789_000_000, task_id),
            )

        lifecycle = proposals.backfill_lifecycle(dry_run=True, conn=conn)
        card = proposals._enriched_items([_load("terminal")], conn=conn)[0]

    assert lifecycle["would_update"] == 1
    assert card["delivery_state"] == "integrated"
    assert card["operator_action_required"] is False


def test_terminal_escalation_is_dismissed_history_not_integrated_delivery(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "terminal-escalation", mode="skill", diff_before_after="", new_text="",
        category="silent_except", theme="silent-except", severity="high",
    )
    with kb.connect() as conn:
        task_id = reconcile._escalate(conn, _load("terminal-escalation"), "operator review required")
        stored = _load("terminal-escalation")
        stored.update({"status": "escalated", "escalation_task_id": task_id})
        proposals.save_proposal(stored)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (1_789_000_000, task_id))

        card = proposals._enriched_items([_load("terminal-escalation")], conn=conn)[0]

    assert card["decision_state"] == "dismissed"
    assert card["delivery_state"] == "none"
    assert card["operator_action_required"] is False


def test_target_change_marks_pending_finding_stale_before_delivery(reconcile_env, tmp_path):
    from hermes_cli import autoresearch_reconcile as reconcile

    target = tmp_path / "target.py"
    target.write_text("before\n", encoding="utf-8")
    _proposal(
        "changed-target", mode="code", finding_id="F-CHANGED",
        target=str(target), target_path=str(target), before_text="before\n",
        category="bug_risk", severity="high",
    )
    target.write_text("after\n", encoding="utf-8")

    with kb.connect() as conn:
        preview = reconcile.reconcile_proposals(conn=conn, dry_run=True)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        actual = reconcile.reconcile_proposals(conn=conn)

    assert preview["stale"] == 1
    assert actual["stale"] == 1
    assert task_count == 0
    stored = _load("changed-target")
    assert stored["status"] == "skipped"
    assert stored["finding_state"] == "stale"


def test_test_foundry_findings_bundle_per_target_without_operator_cards(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    for i in range(2):
        _proposal(
            f"mutation-{i}", mode="test", proposal_type="mutation_test",
            finding_id=f"MUT-{i}", target="hermes_cli/auth.py", target_path="hermes_cli/auth.py",
            category="mutation_survivor", severity="medium",
            evidence=f"surviving mutation {i}", affected_tests=["tests/test_auth.py"],
        )
    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        tasks = conn.execute(
            "SELECT id, idempotency_key FROM tasks WHERE idempotency_key LIKE 'autoresearch:test-foundry:%'"
        ).fetchall()
        comments = conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (tasks[0]["id"],)).fetchall()

    assert summary["routed_to_kanban"] == 2
    assert summary["new_tasks"] == 1
    assert len(tasks) == 1
    assert len(comments) == 1
    assert "[autoresearch-proposal:mutation-" in comments[0]["body"]
    assert proposals.proposals_payload()["open_count"] == 0


def test_existing_task_reuse_bypasses_create_call(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    with kb.connect() as conn:
        existing_id = kb.create_task(
            conn, title="Existing", assignee="coder", created_by="autoresearch",
            idempotency_key="autoresearch:F-REUSE", kind="code", review_tier="review",
        )
    _proposal(
        "reuse", mode="code", finding_id="F-REUSE",
        target="hermes_cli/auth.py", target_path="hermes_cli/auth.py",
        category="bug_risk", severity="high",
    )
    monkeypatch.setattr(reconcile.kb, "create_task", lambda *_a, **_k: pytest.fail("must reuse existing task"))

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)

    assert summary["new_tasks"] == 0
    assert _load("reuse")["kanban_task_id"] == existing_id


def test_archived_task_does_not_bypass_zero_flood_budget(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    with kb.connect() as conn:
        old_id = kb.create_task(
            conn, title="Old", assignee="coder", created_by="autoresearch",
            idempotency_key="autoresearch:F-ARCHIVED", kind="code",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (old_id,))
    _proposal(
        "archived", mode="code", finding_id="F-ARCHIVED",
        target="hermes_cli/auth.py", target_path="hermes_cli/auth.py",
        category="bug_risk", severity="high",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn, max_new_tasks=0)

    assert summary["pooled"] == 1
    assert summary["new_tasks"] == 0
    assert _load("archived")["status"] == "pooled"


def test_veto_operator_escalation_rejects_non_autoresearch_task(reconcile_env):
    """A plain blocked task (no Autoresearch escalation event) must NOT be
    dismissible via this path — a stalled-worker block is not a vetoable signal."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="plain blocked", assignee=None,
            created_by="test", initial_status="blocked", kind="ops",
        )

        vetoed = kb.veto_operator_escalation(conn, task_id)

        assert vetoed is False
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert status == "blocked"
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id = ? AND kind = 'freigabe_vetoed'",
            (task_id,),
        ).fetchone()["n"]
        assert n == 0
